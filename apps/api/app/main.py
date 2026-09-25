from __future__ import annotations

from app.account_api import router as account_router
from app.organization_api import grant_product_to_everyone, router as organization_router
from app.product_knowledge import router as knowledge_router
from app.definitions.loader import parse_definition

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import logging
import re
import threading
import time

from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.auth import (
    AuthUser,
    create_token,
    create_visitor_token,
    demo_identity_allowed,
    demo_login_enabled,
    product_record_grant,
    replace_visitor_token,
    require_any_scope,
    require_auth,
    require_member,
    visitor_from_token,
    require_org_admin,
    require_record_access,
    require_record_admin,
    require_scope,
)
from app.definitions.access import AccessDenied, ProductAccess, authorize_product
from app.definitions.integrity import ReadinessCheck
from app.definitions.sessions import DefinitionUnavailable, SessionEnded, check_pinned_session, pin_new_session
from app.db import get_connection, migrate
from app.engine.composer import receipt_speech
from app.engine.execution import (
    ExecutionLedger,
    ExecutionOwner,
    ExecutionRefused,
    principal_owner,
    require_no_legacy_keys,
)
from dataclasses import replace as dataclasses_replace
from typing import Any

from app.definitions.authoring import store_definition
from app.definitions.console import catch_up_console_products, configured_console
from app.definitions.drafting import ProductDraft, draft_text
from app.definitions.loader import DefinitionError, MAX_DEFINITION_BYTES, parse_definition
from app.definitions.registry import RegistryError
from app.record_access import RecordGrant, grant_records, legacy_record_owner
from app.services.generic_package import DefinitionLookup
from app.services.record_store import (
    PRIMARY,
    RecordConflict as StoreConflict,
    RecordInvalid,
    RecordStore,
    SpaceFull,
)
from app.services.engine_assembly import prepare_turn
from app.services.engine_state import EngineStateStore
from app.services.legacy_adapter import dispatch_legacy_mutation
from app.services import turn_telemetry
from starlette.background import BackgroundTask
from app.schemas import FALLBACK_STAGE, CancelTurnRequest, CancelTurnResponse, ExecutionReceipt, TurnRequest, TurnResponse
from app.record_schemas import (
    CycleInput,
    IssueInput,
    KeyedIssueCreate,
    KeyedIssueUpdate,
    MemberInput,
    ProjectInput,
)
from app.services.agent import DemoAgent
from app.services.product_data_store import (
    InvalidReference,
    ProductDataStore,
    RecordConflict,
    RecordNotFound,
    ScopeViolation,
)
from app.services.demo_refresh import IdleDemoReset
from app.services.demo_instances import (
    DemoInstanceStore,
    InstanceCapacity,
    InstanceConflict,
    InstanceUnavailable,
    SeedNotApproved,
)
from app.installed_products import PackageMissing, package_for
from app.services.rate_limit import RateLimiter
from app.services.record_writes import InvalidChange, KeyedWriter, RecordChange
from app.services.session_manager import utc_now
from app.services.speech_service import SpeechService, SpeechUnavailable
from app.services.env import env_bool, env_value
from app.services.usage_ledger import UsageLedger
from app.product_config import PRODUCTS_BY_ID
from app.tenancy import deployment_id

agent = DemoAgent()
product_data = ProductDataStore()
usage = UsageLedger()
speech_service = SpeechService(usage)
keyed_writes = KeyedWriter()
readiness = ReadinessCheck()
# Armed at startup (see `lifespan`), so tests that never start the server are not throttled.
rate_limits = RateLimiter()
# Legacy member-demo refresh; public visitors use disposable private instances instead.
idle_reset = IdleDemoReset()
logger = logging.getLogger("pixel.api")


class RetentionSchedule:
    """Prunes the execution ledger after a turn's response, at most once a minute."""

    INTERVAL_SECONDS = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: float | None = None

    def schedule(self, add_task) -> bool:
        now = time.monotonic()
        with self._lock:
            if self._last is not None and now - self._last < self.INTERVAL_SECONDS:
                return False
            self._last = now
        add_task(prune_execution_ledger)
        return True


def prune_execution_ledger() -> None:
    """Bounded retention for the execution ledger and durable engine state (5b section 9, 5c 6.3)."""
    try:
        ExecutionLedger.prune()
    except Exception as error:  # noqa: BLE001 - retried at the next trigger
        logger.warning("execution_prune_failed", extra={"error": type(error).__name__})
    try:
        EngineStateStore().prune()
    except Exception as error:  # noqa: BLE001 - retried at the next trigger
        logger.warning("engine_state_prune_failed", extra={"error": type(error).__name__})
    try:
        turn_telemetry.prune()
    except Exception as error:  # noqa: BLE001 - retried at the next trigger
        logger.warning("telemetry_prune_failed", extra={"error": type(error).__name__})


retention = RetentionSchedule()
ENGINE_LEGACY = "legacy"
ENGINE_DEFINITION = "definition"
ENGINE_MODES = frozenset({ENGINE_LEGACY, ENGINE_DEFINITION})
_definition_turns = None
_definition_turns_lock = threading.Lock()
# The shadow engine's controller (5a, reworked in 5b section 10), created on first use and only
# while PIXEL_SHADOW_ENGINE=on. Its lifecycle ends with the application's lifespan.
_shadow = None
_shadow_lock = threading.Lock()
# Whether the last turn saw the switch on; None until a turn has been seen.
_shadow_seen_on: bool | None = None
# When this application instance started serving (set in `lifespan`). A conversation that started
# earlier was not seen by this process's shadow, so its shadow memory is incomplete.
_process_started: datetime | None = None
# The authority selected once at startup (5c plan, section 3.1). Nothing a request carries can
# change it; switching authority is a configuration change and a restart.
_authority: str | None = None


def configured_engine_mode() -> str:
    configured = (env_value("PIXEL_ENGINE_MODE") or ENGINE_LEGACY).strip().lower()
    return configured or ENGINE_LEGACY


def engine_mode() -> str:
    """The authority this process serves: fixed when it started (a test that never starts the
    application reads the configuration directly)."""
    return _authority if _authority is not None else configured_engine_mode()


def engine_mode_problem() -> str | None:
    return None if engine_mode() in ENGINE_MODES else "invalid_engine_mode"


def definition_turns():
    """Definition-engine turn handler, created only when the process-level switch selects it."""
    global _definition_turns
    with _definition_turns_lock:
        if _definition_turns is None:
            from app.services.turn_execution import NewEngineTurns

            _definition_turns = NewEngineTurns(agent.sessions, agent.directory, package_for)
        return _definition_turns


def reset_definition_turns_for_tests() -> None:
    global _definition_turns
    with _definition_turns_lock:
        _definition_turns = None


def shadow_switch_on() -> bool:
    """Only the exact value "on" enables the shadow; unset, empty, "off" or anything else is off."""
    if engine_mode() == ENGINE_DEFINITION:
        return False
    return (env_value("PIXEL_SHADOW_ENGINE") or "").strip().lower() == "on"


def note_shadow_off() -> None:
    global _shadow_seen_on
    _shadow_seen_on = False


def shadow_controller():
    """The process's shadow controller. Turning the switch back on starts a new epoch."""
    global _shadow, _shadow_seen_on
    with _shadow_lock:
        if _shadow is None:
            from app.services.shadow import ShadowController, ShadowRunner

            started = _process_started or datetime.now(UTC)
            _shadow = ShadowController(ShadowRunner(
                agent.directory, agent.sessions.pin_for, package_for, epoch_started=started,
            ))
        elif _shadow_seen_on is False:
            _shadow.new_epoch(datetime.now(UTC))
        _shadow_seen_on = True
        return _shadow


def live_turn(request: TurnRequest, user: AuthUser, visible_data: dict, grant: RecordGrant) -> TurnResponse:
    """The legacy engine's turn; retained as the rollback path until 5d.

    Its create/update decisions are dispatched through the same 5b execution ledger the definition
    engine uses (5c plan, section 3.3): rollback restores navigation and highlighting, never a
    keyless assistant write.
    """
    response = agent.handle_turn(request, user, visible_data)
    return dispatch_legacy_mutation(response, request, user, sessions=agent.sessions, directory=agent.directory)


def turn_engine():
    mode = engine_mode()
    if mode == ENGINE_LEGACY:
        return live_turn
    if mode == ENGINE_DEFINITION:
        return definition_turns()
    raise HTTPException(status_code=503, detail="Invalid engine authority mode.")


def close_shadow() -> None:
    global _shadow
    with _shadow_lock:
        controller, _shadow = _shadow, None
    if controller is not None:
        # Sheds queued work, stops the worker and watchdog briefly, and flushes counts; a failure
        # is logged, never raised.
        controller.close()



@asynccontextmanager
async def lifespan(_: FastAPI):
    global _process_started, _authority
    _process_started = datetime.now(UTC)
    _authority = configured_engine_mode()
    logger.info("engine_authority", extra={"authority": _authority})
    was_armed = rate_limits.armed
    reset_was_armed = idle_reset.armed
    try:
        migrate()
        # 5b: refuse to serve while a workspace-less key is still dispatched, before pruning can erase
        # the evidence (plan, section 7.1), then prune retained execution rows.
        require_no_legacy_keys()
        prune_execution_ledger()
        DemoInstanceStore().prune(limit=1000)
        rate_limits.arm()
        idle_reset.arm()
        # Anyone who has not signed in since the last release still gets the assistant this
        # deployment ships, rather than the one that existed when they signed up.
        try:
            catch_up_console_products()
        except Exception:  # noqa: BLE001 - starting up matters more than catching up
            logger.exception("console_catch_up_unavailable")
        if env_bool("PIXEL_DEMO_SEEDS", default=False):
            # Demo records are created once, at startup, and never by a request: a refused or
            # unauthorized request must leave product data exactly as it found it.
            product_data.seed_if_empty()
        yield
    finally:
        _authority = None
        close_shadow()
        # A TestClient owns only the lifecycle state it started. Production remains armed until
        # process shutdown; a temporary test server must not throttle later direct-app tests.
        if not was_armed:
            rate_limits.disarm()
        if not reset_was_armed:
            idle_reset.disarm()


app = FastAPI(
    title="Pixel Demo Agent API",
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(account_router)
app.include_router(organization_router)
app.include_router(knowledge_router)


@app.exception_handler(RecordConflict)
async def conflict_handler(_: Request, error: RecordConflict):
    return JSONResponse(status_code=409, content={"detail": str(error)})


@app.exception_handler(RequestValidationError)
async def refused_request_handler(_: Request, error: RequestValidationError):
    """Say what was wrong with a request in a sentence, naming the field it was wrong about.

    The default answer is a validation report: a list of objects, each carrying the whole body
    that was sent, written for whoever is holding the schema. Our clients are screens people use,
    and this reaches them - a person who typed a product name has no idea what
    `identity.product_name` is, and should never be shown their own input echoed back as `input`.

    The status is unchanged, and `detail` is still a string, so nothing that reads it breaks.
    """
    first = (error.errors() or [{}])[0]
    location = [str(part) for part in first.get("loc", ()) if part not in ("body", "query", "path")]
    field = location[-1] if location else None
    message = str(first.get("msg", "This request could not be accepted.")).removeprefix("Value error, ")
    # A rule that checked one field of a whole body says so itself, because the body is what
    # failed and only the rule knows which part of it did.
    named, _, attributed = message.partition(": ")
    if attributed and " " not in named:
        field, message = named, attributed
        return JSONResponse(status_code=422,
                            content={"detail": f"{message.rstrip('.')}.", "field": field})
    if field is None:
        return JSONResponse(status_code=422, content={"detail": message, "field": None})
    _, label = _DRAFT_FIELDS.get(".".join(location[-2:]), (None, None))
    label = label or field.replace("_", " ").capitalize()
    body = message[0].lower() + message[1:] if message[:1].isupper() else message
    return JSONResponse(status_code=422,
                        content={"detail": f"{label} {body.rstrip('.')}.", "field": field})


@app.exception_handler(RecordNotFound)
async def missing_handler(_: Request, error: RecordNotFound):
    return JSONResponse(status_code=404, content={"detail": str(error)})


@app.exception_handler(ScopeViolation)
async def scope_violation_handler(_: Request, error: ScopeViolation):
    return JSONResponse(status_code=403, content={"detail": str(error)})


@app.exception_handler(ExecutionRefused)
async def execution_refused_handler(_: Request, error: ExecutionRefused):
    # An unrecognized key is a plain 404 that reveals nothing; a caller whose standing changed is
    # a 403; any other unusable key is a conflict. None of these carries a receipt.
    if error.not_found:
        return JSONResponse(status_code=404, content={"detail": "This change was not found."})
    return JSONResponse(status_code=409 if error.conflict else 403, content={"detail": error.reason})


@app.exception_handler(InvalidReference)
async def invalid_reference_handler(_: Request, error: InvalidReference):
    return JSONResponse(status_code=422, content={"detail": str(error)})


@app.exception_handler(InstanceUnavailable)
async def private_instance_missing_handler(_: Request, __: InstanceUnavailable):
    return JSONResponse(status_code=404, content={"detail": "This demo is unavailable."})


@app.exception_handler(InstanceConflict)
async def private_instance_conflict_handler(_: Request, error: InstanceConflict):
    return JSONResponse(status_code=409, content={"detail": str(error)})


@app.exception_handler(InstanceCapacity)
async def private_instance_capacity_handler(_: Request, error: InstanceCapacity):
    return JSONResponse(
        status_code=429, content={"detail": str(error)}, headers={"Retry-After": "60"}
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Unauthenticated endpoints ---


@app.get("/health")
@app.get("/api/health")
def health():
    # Readiness includes storage. A process that cannot open its persistent database must not be
    # advertised to the web app as healthy.
    with get_connection() as connection:
        connection.execute("select 1").fetchone()
    mode_problem = engine_mode_problem()
    if mode_problem is not None:
        logger.warning("engine_mode_unavailable", extra={"reason": mode_problem, "configured": engine_mode()})
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": mode_problem},
        )
    # Readiness also includes being able to start a conversation. A backend that answers every
    # turn with "not available" is not healthy, however well its database opens.
    problems = readiness.problems()
    if problems:
        return JSONResponse(
            status_code=503,
            content={"status": "unhealthy", "reason": "sessions_cannot_start", "products": len(problems)},
        )
    # The serving authority is reported so readiness can be checked against the configured mode
    # (5c plan, sections 3.1 and 11.4). No secret or identifier is included.
    return {"status": "ok", "authority": engine_mode(), "shadow": "on" if shadow_switch_on() else "off"}


class DemoLoginRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=200)


class DemoLoginResponse(BaseModel):
    token: str
    user_id: str
    tenant_id: str


@app.post("/api/auth/demo-login", response_model=DemoLoginResponse)
def demo_login(body: DemoLoginRequest, http: Request) -> DemoLoginResponse:
    rate_limits.enforce("login", http)
    if not demo_login_enabled():
        return JSONResponse(status_code=403, content={"detail": "Demo login is disabled."})
    organizations = agent.directory.organizations_of(body.user_id)
    # One answer for unknown users and for identities the demo may not issue, so the endpoint
    # never confirms that an administrator exists.
    if len(organizations) != 1 or not demo_identity_allowed(body.user_id, organizations[0]):
        return JSONResponse(status_code=404, content={"detail": "Unknown demo user."})
    # A new visitor starts from the seed if earlier visitors changed it and then left.
    idle_reset.before_sign_in(product_data.reset)
    tenant_id = organizations[0]
    token = create_token(body.user_id, tenant_id)
    return DemoLoginResponse(token=token, user_id=body.user_id, tenant_id=tenant_id)


class ProductSummary(BaseModel):
    team_id: str = ""
    """One product of the caller's organization, as the console lists it."""

    product_id: str
    name: str
    definition_id: str
    definition_version: int
    state: str
    visitor_access: bool
    entities: list[str]
    views: list[str]


class ProductsResponse(BaseModel):
    tenant_id: str
    products: list[ProductSummary]


class AddProductRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str = Field(min_length=1, max_length=64)
    team_id: str = Field(min_length=1, max_length=64)
    definition_id: str = Field(min_length=1, max_length=64)
    # The definition itself, as its author wrote it. It is parsed and validated before anything
    # is stored: a product nobody can describe is not a product Pixel will run.
    definition: str = Field(min_length=1, max_length=MAX_DEFINITION_BYTES)
    definition_version: int = Field(default=1, ge=1)


class MemberSummary(BaseModel):
    """One person in an organization, as the console lists them."""

    user_id: str
    email: str | None = None
    role: str
    team_id: str | None = None
    team_name: str | None = None


class MembersResponse(BaseModel):
    tenant_id: str
    members: list[MemberSummary]


@app.get("/api/organizations/{tenant_id}/members", response_model=MembersResponse)
def list_members(tenant_id: str, user: AuthUser = Depends(require_member)) -> MembersResponse:
    """Everyone in the caller's own organization. Never another organization's.

    A screen that shows people has to show the ones who are really there. Leaving a sample list in
    place once an account is real tells somebody their colleagues are in Pixel when they are not.
    """
    if tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="That organization is not available to you.")
    directory = agent.directory
    teams = {team.team_id: team.name for team in directory.teams(tenant_id)}
    with get_connection() as connection:
        emails = {row["user_id"]: row["email"] for row in connection.execute(
            "select user_id, email from email_accounts where tenant_id = ?", (tenant_id,)
        ).fetchall()}
    return MembersResponse(tenant_id=tenant_id, members=[
        MemberSummary(user_id=member.user_id, email=emails.get(member.user_id),
                      role=member.role, team_id=member.team_id,
                      team_name=teams.get(member.team_id) if member.team_id else None)
        for member in directory.members(tenant_id)
    ])


@app.get("/api/organizations/{tenant_id}/products", response_model=ProductsResponse)
def list_products(tenant_id: str, user: AuthUser = Depends(require_member)) -> ProductsResponse:
    """Every product of the caller's own organization. Never another organization's."""
    if tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="That organization is not available to you.")
    directory = agent.directory
    # Pixel itself is not one of somebody's products, however it is served; listing it would
    # invite them to open the application inside the application.
    console = configured_console()
    summaries = []
    for binding in directory.active_products():
        if binding.tenant_id != tenant_id:
            continue
        if console is not None and binding.product_id == console.product_id:
            continue
        try:
            authorize_product(user, binding.product_id, directory)
        except AccessDenied:
            continue
        try:
            definition = directory.definitions.load(
                binding.definition_id, binding.definition_version).definition
        except Exception:
            # A product whose definition cannot be read is listed plainly rather than hidden, so
            # an operator can see that it needs attention.
            summaries.append(ProductSummary(
                team_id=binding.team_id,
                product_id=binding.product_id, name=binding.product_id,
                definition_id=binding.definition_id, definition_version=binding.definition_version,
                state="unreadable", visitor_access=binding.visitor_access, entities=[], views=[]))
            continue
        summaries.append(ProductSummary(
            team_id=binding.team_id,
            product_id=binding.product_id,
            name=definition.identity.product_name,
            definition_id=binding.definition_id,
            definition_version=binding.definition_version,
            state=binding.state,
            visitor_access=binding.visitor_access,
            entities=sorted(definition.entities),
            views=sorted(definition.views),
        ))
    return ProductsResponse(tenant_id=tenant_id, products=summaries)


class DraftedDefinition(BaseModel):
    """A definition written from a description, for the person who described it to read."""

    definition_id: str
    product_name: str
    definition: str
    things: list[str]
    screens: list[str]
    can_do: list[str]


@app.post("/api/product-drafts", response_model=DraftedDefinition)
def draft_product(draft: ProductDraft, user: AuthUser = Depends(require_member)) -> DraftedDefinition:
    """Write a definition from a description of a product, and change nothing.

    Nothing is stored and no product is created: this answers "here is what I understood", so the
    person who described it can read it before it becomes a product anyone can talk to.
    """
    try:
        text = draft_text(draft, user.tenant_id)
        definition = parse_definition(text.encode())
    except (DefinitionError, ValueError) as refused:
        raise HTTPException(status_code=422, detail=_draft_problem(refused)) from refused
    return DraftedDefinition(
        definition_id=draft.definition_id,
        product_name=definition.identity.product_name,
        definition=text,
        things=[entity.plural for entity in definition.entities.values()],
        screens=[view.label for view in definition.views.values()],
        can_do=sorted({action.description for action in definition.actions.values()}),
    )


# Where a definition's own field names come from, in the words somebody filled in. A person who
# typed a name into a box called "Product name" should not be sent to read about
# `identity.product_name`, which appears nowhere they have been.
_DRAFT_FIELDS = {
    "identity.product_name": ("product_name", "The product name"),
    "identity.assistant_name": ("assistant_name", "The assistant's name"),
    "definition.definition_id": ("definition_id", "The product's address"),
}
_DRAFT_PROBLEM = re.compile(r"Value error, ([a-z_]+\.[a-z_]+): (.+?)(?: \[type=|$)", re.S)


def _draft_problem(refused: Exception) -> dict:
    """What was wrong with a described product, said once and attributed to one field.

    A validation report is written for whoever wrote the definition. Nobody wrote this one: Pixel
    did, from what somebody typed, so the report is ours to read and theirs to be told about in a
    sentence. Anything we cannot attribute is still returned, plainly, rather than swallowed.
    """
    found = _DRAFT_PROBLEM.search(str(refused))
    if found is None:
        return {"field": None, "message": "This product could not be written. Please check what you entered."}
    path, problem = found.group(1), " ".join(found.group(2).split())
    # One field can fail several rules at once. The first is the one to fix, and the rest repeat
    # the field's own path, which is ours and means nothing to the person reading it.
    problem = problem.split(";")[0].strip().rstrip(".")
    field, label = _DRAFT_FIELDS.get(path, (None, path.rpartition(".")[2].replace("_", " ").capitalize()))
    return {"field": field, "message": f"{label} {problem}."}


@app.post("/api/organizations/{tenant_id}/products", response_model=ProductSummary, status_code=201)
def add_product(tenant_id: str, body: AddProductRequest, http: Request,
                user: AuthUser = Depends(require_member)) -> ProductSummary:
    """Add a product to this organization from a definition, with no code and no deployment."""
    if tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="That organization is not available to you.")
    if user.kind != "member" or not (
        user.role == "org_admin" or (user.role == "team_admin" and user.team_id == body.team_id)
    ):
        raise HTTPException(status_code=403, detail="Only an organization admin or this team's admin can add products.")
    rate_limits.enforce("write", http, identity=user.user_id)
    directory = agent.directory
    try:
        identity = parse_definition(body.definition.encode("utf-8")).definition
        if identity.ownership != "organization_private" or identity.owner_organization != tenant_id:
            raise HTTPException(status_code=403, detail="Uploaded definitions must be private to your organization.")
        store_definition(body.definition, definition_id=body.definition_id,
                         version=body.definition_version)
        directory.definitions.ensure_published(body.definition_id, body.definition_version)
        binding = directory.bind_product(tenant_id, body.product_id, body.team_id,
                                         body.definition_id, body.definition_version)
    except (DefinitionError, RegistryError) as refused:
        raise HTTPException(status_code=400, detail=str(refused)) from refused
    # Whoever added the product can work in it straight away; a product nobody may open is not
    # one anybody added on purpose.
    grant_records(tenant_id, body.product_id, user.user_id, [], True)
    # And so can everyone else already in the organization, each on their own terms.
    grant_product_to_everyone(tenant_id, body.product_id, directory)
    definition = directory.definitions.load(body.definition_id, body.definition_version).definition
    return ProductSummary(
        team_id=binding.team_id,
        product_id=binding.product_id, name=definition.identity.product_name,
        definition_id=binding.definition_id, definition_version=binding.definition_version,
        state=binding.state, visitor_access=binding.visitor_access,
        entities=sorted(definition.entities), views=sorted(definition.views),
    )


class RecordCreate(BaseModel):
    """A record to create, either proposed by the assistant or filled in by the person.

    A proposal names the action its key bound; a form does not, because nothing proposed it. The
    two are told apart by the execution key, and the keyed path still refuses a body without the
    action it was supposed to carry.
    """

    model_config = ConfigDict(extra="forbid")

    action: str | None = Field(default=None, min_length=1, max_length=64)
    fields: dict[str, Any] = Field(default_factory=dict)


class DirectRecordChange(BaseModel):
    """An edit somebody made on a form, against the version of the record they were shown."""

    model_config = ConfigDict(extra="forbid")

    changes: dict[str, Any] = Field(min_length=1)
    revision: int = Field(ge=1)


class KeyedRecordChange(BaseModel):
    """A change the assistant proposed: the action it named and only the fields its key bound."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=64)
    changes: dict[str, Any] = Field(min_length=1)


def _product_definition(user: AuthUser, product_id: str):
    """The definition this product runs now. A product the caller may not reach is reported the
    same way as one that does not exist."""
    binding = agent.directory.product(user.tenant_id or "", product_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="That product is not available to you.")
    try:
        return agent.directory.definitions.load(
            binding.definition_id, binding.definition_version).definition
    except (DefinitionError, RegistryError) as unavailable:
        raise HTTPException(status_code=409, detail="This product is being changed.") from unavailable


def _record_space(user: AuthUser) -> str:
    """Whose records these are: a visitor's own demo instance, or the organization's own."""
    context = getattr(user, "demo_context", None)
    return getattr(context, "instance_id", None) or PRIMARY


def _visible_record(definition, store: "RecordStore", grant: RecordGrant, scope_id: str,
                    entity: str, record_id: str):
    """The record as this caller sees it in one workspace, or None when they cannot see it."""
    narrowed = dataclasses_replace(grant, scope_ids=frozenset({scope_id}), is_admin=False)
    return DefinitionLookup(definition, store, narrowed.visible_scope_ids()).get(entity, record_id)


class FieldShape(BaseModel):
    """One field as a screen needs to show and edit it."""

    name: str
    label: str
    type: str
    required: bool
    editable: bool
    display: bool
    values: list[str] = []
    target: str | None = None


class EntityShape(BaseModel):
    name: str
    label: str
    plural: str
    title_field: str
    summary_fields: list[str] = []
    fields: list[FieldShape] = []
    # Whether these records are the product's people, so a screen can show them as people.
    is_people: bool = False


class ControlShape(BaseModel):
    name: str
    label: str


class ViewShape(BaseModel):
    name: str
    label: str
    kind: str
    entity: str | None = None
    shortcut: str | None = None
    navigable: bool = True
    columns: list[str] = []
    controls: list[ControlShape] = []


class ActionShape(BaseModel):
    name: str
    client_type: str
    capability: str
    description: str
    entity: str | None = None
    view: str | None = None
    fields: list[str] = []
    by: str | None = None
    control: str | None = None


class ProductShape(BaseModel):
    """Everything a screen needs to render a product it has never seen before.

    The shape of a product, never its records: what its things are called, what fields they
    carry, what screens it has and what sits on them. A client that can render this can render
    any product the platform runs.
    """

    product_id: str
    product_name: str
    assistant_name: str
    definition_id: str
    definition_version: int
    views: list[ViewShape]
    entities: list[EntityShape]
    actions: list[ActionShape]


class RecordsResponse(BaseModel):
    product_id: str
    scope: str
    records: dict[str, list[dict[str, Any]]]


@app.get("/api/products/{product_id}/shape", response_model=ProductShape)
def product_shape(product_id: str, user: AuthUser = Depends(require_auth)) -> ProductShape:
    """How to render this product. Its records are asked for separately."""
    binding = agent.directory.product(user.tenant_id or "", product_id)
    definition = _product_definition(user, product_id)
    return ProductShape(
        product_id=product_id,
        product_name=definition.identity.product_name,
        assistant_name=definition.identity.assistant_name,
        definition_id=binding.definition_id,
        definition_version=binding.definition_version,
        views=[
            ViewShape(
                name=name, label=view.label, kind=view.kind, entity=view.entity,
                shortcut=view.shortcut, navigable=view.navigable, columns=list(view.columns),
                controls=[ControlShape(name=key, label=control.label)
                          for key, control in view.controls.items()],
            )
            for name, view in definition.views.items()
        ],
        entities=[
            EntityShape(
                name=name, label=entity.label, plural=entity.plural,
                title_field=entity.title_field, summary_fields=list(entity.summary_fields),
                is_people=bool(definition.people and definition.people.entity == name),
                fields=[
                    FieldShape(
                        name=field_name, label=spec.label or field_name.replace("_", " "),
                        type=spec.type, required=spec.required, editable=spec.editable,
                        display=spec.display, values=list(spec.values), target=spec.target,
                    )
                    for field_name, spec in entity.fields.items()
                ],
            )
            for name, entity in definition.entities.items()
        ],
        actions=[
            ActionShape(
                name=name, client_type=name.upper(), capability=str(action.capability),
                description=action.description, entity=action.entity, view=action.view,
                fields=list(action.fields),
                by=action.by, control=action.control,
            )
            for name, action in definition.actions.items()
        ],
    )


@app.get("/api/products/{product_id}/records", response_model=RecordsResponse)
def read_product_records(product_id: str, workspace_scope_id: str | None = None,
                         user: AuthUser = Depends(require_auth)) -> RecordsResponse:
    """Every record of this product the caller may see, grouped by the kind of thing it is.

    Read through the product's own package, exactly as a turn of conversation reads it. A product
    that brought its own record source is served from that source; one that is only a definition
    is served from the generic store. Reading the generic store for every product was wrong for
    the first kind: the screen showed nothing while the assistant, reading the other way, answered
    about records that were plainly there. A screen and an assistant that disagree about the same
    product are worse than either being wrong alone.

    Each record carries the version it is at, so an edit can say which version it was made
    against. A product whose records come from somewhere without versions reports 0, which no
    edit will be accepted for: such a product is changed where its records actually live.
    """
    definition = _product_definition(user, product_id)
    grant = product_record_grant(user, product_id)
    if workspace_scope_id is not None:
        require_scope(workspace_scope_id, grant)
    # The same snapshot a turn is given, including the legacy records for whichever product still
    # owns them. A source that does not use them ignores them; none of them can widen what a
    # caller sees, because the scope narrowing has already happened on the grant.
    visible_data = _product_data(grant).load(grant.visible_scope_ids())
    prepared = prepare_turn(agent.directory, package_for, user, grant, product_id,
                            visible_data, workspace_scope_id, definition)
    if prepared is None:
        raise HTTPException(status_code=404, detail="That product is not available to you.")
    store = RecordStore(user.tenant_id or "", product_id, _record_space(user))
    revisions = {(entity, record.id): record.revision
                 for entity, records in store.all().items() for record in records}
    return RecordsResponse(
        product_id=product_id,
        scope=prepared.scope_label,
        records={
            entity: [{"id": view.id, "title": view.title,
                      "revision": revisions.get((entity, view.id), 0), **dict(view.fields)}
                     for view in views]
            for entity, views in prepared.records.items()
        },
    )


@app.post("/api/products/{product_id}/records/{entity}")
def create_product_record(product_id: str, entity: str, body: RecordCreate, http: Request,
                          user: AuthUser = Depends(require_auth),
                          x_execution_key: str | None = Header(default=None, max_length=200),
                          x_session_id: str | None = Header(default=None, max_length=100)):
    """Create one record of any product.

    With an execution key this is the assistant's proposal being carried out, and the key decides
    which change may be made and in whose workspace. Without one it is a person filling in a form,
    where the values came from them directly and the only question is whether they may put a
    record where this one would land. Both go through the same validation and the same store; what
    differs is where the values came from, and only the keyed path can be replayed.
    """
    rate_limits.enforce("write", http, identity=user.user_id)
    definition = _product_definition(user, product_id)
    if entity not in definition.entities:
        raise HTTPException(status_code=404, detail="That product has no such records.")
    fields = dict(body.fields)
    store = RecordStore(user.tenant_id or "", product_id, _record_space(user))

    if x_execution_key is None:
        return _direct_record(user, product_id, definition, store, entity,
                              lambda connection: store.create(definition, entity, fields,
                                                              connection=connection),
                              created=True)

    if not body.action:
        raise HTTPException(status_code=422, detail="A keyed create names the action it carries out.")

    def apply(connection, grant: RecordGrant, scope_id: str) -> RecordChange:
        try:
            record = store.create(definition, entity, fields, connection=connection)
        except (RecordInvalid, StoreConflict, SpaceFull) as refused:
            raise InvalidChange(str(refused)) from refused
        return RecordChange(record.id, {"id": record.id, **dict(record.fields)})

    return _keyed_record(x_execution_key, x_session_id or "", user, product_id, definition, store,
                         {"action": body.action, "entity": entity, "fields": fields},
                         apply, entity, created=True)


@app.put("/api/products/{product_id}/records/{entity}/{record_id}")
def edit_product_record(product_id: str, entity: str, record_id: str, body: DirectRecordChange,
                        http: Request, user: AuthUser = Depends(require_auth)):
    """Change one record of any product from a form the person filled in themselves.

    The edit names the version of the record it was made against. If that version has moved on -
    somebody else changed it, or the assistant did - the edit is refused and the record as it is
    now goes back with the refusal, so the person decides what to do rather than losing somebody
    else's work to a save button. Assistant changes never come through here; they use PATCH with
    the key that bound them.
    """
    rate_limits.enforce("write", http, identity=user.user_id)
    definition = _product_definition(user, product_id)
    if entity not in definition.entities:
        raise HTTPException(status_code=404, detail="That product has no such records.")
    changes = dict(body.changes)
    store = RecordStore(user.tenant_id or "", product_id, _record_space(user))
    return _direct_record(user, product_id, definition, store, entity,
                          lambda connection: store.update(definition, entity, record_id, changes,
                                                          expected_revision=body.revision,
                                                          connection=connection),
                          target=record_id)


def _direct_record(user: AuthUser, product_id: str, definition, store: "RecordStore", entity: str,
                   change, *, created: bool = False, target: str | None = None) -> JSONResponse:
    """One record written by the person themselves, inside their own reach.

    A record is only written if the person can see it afterwards. That is the whole access rule:
    creating a record somewhere they cannot reach, or moving one out of their reach, would be a
    way of writing into a workspace that is not theirs, so the write is rolled back instead. The
    check runs in the same transaction as the change, so nothing can move in between.
    """
    if user.kind == "member":
        idle_reset.touched(changed=True)
    grant = product_record_grant(user, product_id)
    scope_ids = grant.visible_scope_ids()

    def visible(record_id: str, connection):
        """Read inside the write's own transaction, so a record just written is judged as it
        will be once it is committed rather than as it was before it existed."""
        return DefinitionLookup(definition, store, scope_ids,
                                connection=connection).get(entity, record_id)

    try:
        with get_connection() as connection:
            connection.execute("begin immediate")
            if target is not None and visible(target, connection) is None:
                raise RecordNotFound("That record is unavailable.")
            record = change(connection)
            if visible(record.id, connection) is None:
                raise ScopeViolation("That record would be outside your workspace.")
            content = {"id": record.id, "revision": record.revision, **dict(record.fields)}
    except RecordInvalid as invalid:
        raise HTTPException(status_code=422, detail=str(invalid)) from invalid
    except SpaceFull as full:
        raise HTTPException(status_code=409, detail=str(full)) from full
    except StoreConflict as conflict:
        raise HTTPException(status_code=409, detail=_conflict_detail(
            str(conflict), definition, store, scope_ids, entity, target)) from conflict
    except RecordNotFound as missing:
        raise HTTPException(status_code=404, detail=str(missing)) from missing
    except ScopeViolation as refused:
        raise HTTPException(status_code=403, detail=str(refused)) from refused
    return JSONResponse(status_code=201 if created else 200, content=content)


def _conflict_detail(message: str, definition, store: "RecordStore", scope_ids, entity: str,
                     record_id: str | None) -> dict:
    """What a refused edit is told: why, and the record as it stands now if they may see it."""
    current = DefinitionLookup(definition, store, scope_ids).get(entity, record_id or "")
    if current is None:
        return {"message": message, "record": None}
    held = store.get(entity, current.id)
    return {"message": message,
            "record": {"id": current.id, "revision": held.revision if held else 0,
                       **dict(current.fields)}}


@app.patch("/api/products/{product_id}/records/{entity}/{record_id}")
def change_product_record(product_id: str, entity: str, record_id: str, body: KeyedRecordChange,
                          http: Request, user: AuthUser = Depends(require_auth),
                          x_execution_key: str = Header(max_length=200),
                          x_session_id: str = Header(max_length=100)):
    """Change one record of any product: exactly what the key bound, and nothing else."""
    rate_limits.enforce("write", http, identity=user.user_id)
    definition = _product_definition(user, product_id)
    if entity not in definition.entities:
        raise HTTPException(status_code=404, detail="That product has no such records.")
    changes = dict(body.changes)
    store = RecordStore(user.tenant_id or "", product_id, _record_space(user))

    def apply(connection, grant: RecordGrant, scope_id: str) -> RecordChange:
        # The record must be one this caller can see in the key's own workspace. One they cannot
        # see is reported as missing, never as forbidden.
        if _visible_record(definition, store, grant, scope_id, entity, record_id) is None:
            raise RecordNotFound("That record is unavailable.")
        try:
            record = store.update(definition, entity, record_id, changes, connection=connection)
        except RecordInvalid as invalid:
            raise InvalidChange(str(invalid)) from invalid
        return RecordChange(record.id, {"id": record.id, **dict(record.fields)})

    return _keyed_record(x_execution_key, x_session_id, user, product_id, definition, store,
                         {"action": body.action, "entity": entity, "target": record_id,
                          "changes": changes},
                         apply, entity)


def _keyed_record(execution_key: str, session_id: str, user: AuthUser, product_id: str,
                  definition, store: "RecordStore", change_set: dict, apply, entity: str,
                  *, created: bool = False) -> JSONResponse:
    """One keyed write on any product's records, answered with its receipt."""
    if user.kind == "member":
        idle_reset.touched(changed=True)

    def reload(connection, grant: RecordGrant, scope_id: str, record_id: str) -> dict | None:
        view = _visible_record(definition, store, grant, scope_id, entity, record_id)
        return {"id": view.id, **dict(view.fields)} if view is not None else None

    result = keyed_writes.write(
        apply, execution_key=execution_key, principal=user, product_id=product_id,
        session_id=session_id, change_set=change_set, reload=reload,
    )
    executed = result.outcome == "executed"
    receipt = ExecutionReceipt(
        outcome=result.outcome,
        code=result.code,
        speech=receipt_speech(
            result.code, executed=executed, replay=result.replay, created=created,
            record_id=result.record_id,
            changes=_committed_values(change_set.get("changes") or change_set.get("fields"),
                                      result.record),
            title_field=definition.entities[entity].title_field if entity in definition.entities else "title",
        ),
        record=result.record,
    )
    pin = agent.sessions.pin_for(session_id) if session_id else None
    telemetry = BackgroundTask(
        turn_telemetry.record, deployment_id=deployment_id(), tenant_id=user.tenant_id or "",
        product_id=product_id, definition_version=pin.definition_version if pin else None,
        authority=engine_mode(),
        counts={"receipt": "replayed" if result.replay else result.outcome,
                "receipt_code": result.code},
    )
    return JSONResponse(status_code=200 if executed else 409,
                        content=receipt.model_dump(mode="json"), background=telemetry)


class VisitorSessionResponse(BaseModel):
    token: str
    visitor_id: str
    tenant_id: str
    product_id: str
    instance_id: str
    generation: int


@app.post(
    "/api/organizations/{tenant_id}/products/{product_id}/visitor-sessions",
    response_model=VisitorSessionResponse,
)
def start_visitor_session(tenant_id: str, product_id: str, http: Request) -> VisitorSessionResponse:
    """A visitor session is scoped to one product and grants nothing else."""
    rate_limits.enforce("login", http)
    try:
        token, visitor_id = create_visitor_token(tenant_id, product_id)
    except AccessDenied:
        # One answer for unknown, private, disabled and suspended alike.
        raise HTTPException(status_code=404, detail="This product is not available.")
    # Resolve through the persisted login row rather than trusting anything from the request.
    visitor = visitor_from_token(token)
    return VisitorSessionResponse(
        token=token, visitor_id=visitor_id, tenant_id=tenant_id, product_id=product_id,
        instance_id=visitor.instance_id or "", generation=visitor.instance_generation or 0,
    )


# --- Authenticated endpoints ---


# Legacy record endpoints serve only the designated record-owning product (until step 3.5).


@app.get("/api/demo-data")
def get_demo_data(grant: RecordGrant = Depends(require_record_access)) -> dict[str, list[dict]]:
    if grant.demo_context is None:
        idle_reset.touched()
    return _product_data(grant).load(grant.visible_scope_ids())


@app.post("/api/demo-data/reset")
def reset_demo_data(http: Request, user: AuthUser = Depends(require_member),
                    grant: RecordGrant = Depends(require_record_access)) -> dict[str, list[dict]]:
    """Resets everyone's demo data, so it belongs to a record administrator only.

    The public demo identity is never one (see `demo_identity_allowed`); operators reset from the
    server with `python -m app.ops reset-demo-data`.
    """
    rate_limits.enforce("reset", http, identity=user.user_id)
    require_record_admin(grant)
    data = product_data.reset()
    idle_reset.restored()
    return data


class PrivateDemoResetResponse(BaseModel):
    token: str
    instance_id: str
    generation: int
    data: dict[str, list[dict]]


@app.post("/api/demo-data/reset-mine", response_model=PrivateDemoResetResponse)
def reset_private_demo(http: Request, user: AuthUser = Depends(require_auth)) -> PrivateDemoResetResponse:
    """Restore only this visitor's synthetic records and invalidate the old generation."""
    context = user.demo_context
    if context is None or not user.product_id:
        raise HTTPException(status_code=403, detail="This operation requires a private demo visitor.")
    rate_limits.enforce("reset", http, identity=user.user_id)
    access = authorize_product(user, user.product_id, agent.directory)
    try:
        package = package_for(access.binding.definition_id)
    except PackageMissing:
        raise HTTPException(status_code=409, detail="This demo cannot be restored.")
    if package.demo_seed_factory is None:
        raise HTTPException(status_code=409, detail="This demo cannot be restored.")
    try:
        seed = package.demo_seed_factory()
    except SeedNotApproved:
        raise HTTPException(status_code=409, detail="This demo cannot be restored.")
    records = ProductDataStore(context)
    with get_connection() as connection:
        connection.execute("begin immediate")
        updated, data = records.reset_private(seed, connection=connection)
        # A delayed turn or execution from the old generation can no longer affect reset data.
        session_rows = connection.execute(
            "select session_id from conversation_owners where user_id=? and customer_id=? "
            "and product_id=? and instance_id=? and instance_generation=?",
            (user.user_id, user.tenant_id, user.product_id, context.instance_id, context.generation),
        ).fetchall()
        session_ids = [row["session_id"] for row in session_rows]
        for session_id in session_ids:
            connection.execute(
                "update sessions set active_turn_id=null, ended_at=? where id=?",
                (utc_now(), session_id),
            )
        ExecutionLedger().cancel_instance(connection, ExecutionOwner(
            user.tenant_id, user.product_id, user.user_id, context.instance_id, context.generation,
        ))
        EngineStateStore().invalidate_sessions(connection, session_ids)
        token = replace_visitor_token(user, updated, seed.version, connection)
    return PrivateDemoResetResponse(
        token=token, instance_id=updated.instance_id, generation=updated.generation, data=data
    )


def _product_data(grant: RecordGrant) -> ProductDataStore:
    return ProductDataStore(grant.demo_context) if grant.demo_context else product_data


@app.post("/api/demo-data/issues")
def create_demo_issue(http: Request,
                      body: dict = Body(...),
                      user: AuthUser = Depends(require_auth),
                      idempotency_key: str | None = Header(default=None, max_length=200),
                      x_execution_key: str | None = Header(default=None, max_length=200),
                      x_session_id: str | None = Header(default=None, max_length=100)):
    rate_limits.enforce("write", http, identity=user.user_id)
    if user.kind == "member":
        idle_reset.touched(changed=True)
    if x_execution_key:
        _refuse_legacy_idempotency(idempotency_key)
        # An assistant create carries only the fields its key bound; the server assigns the ID
        # and the workspace fields from the key's workspace (5b plan, section 7.1).
        keyed = _validated(KeyedIssueCreate, body)
        fields = dict(keyed.fields)
        return _keyed(x_execution_key, x_session_id, user, {"action": "create_issue", "fields": fields},
                      lambda product_id: _create_issue(fields, user, product_id), created=True)
    issue = _validated(IssueInput, body)
    payload = _record_payload(issue)
    grant = require_record_access(user)
    records = _product_data(grant)
    require_any_scope(records.scopes_for_record(issue.projectId, issue.project), grant)
    return records.save_issue(payload, idempotency_key)


@app.put("/api/demo-data/issues/{issue_id}")
def update_demo_issue(issue_id: str, issue: IssueInput,
                      http: Request,
                      user: AuthUser = Depends(require_auth),
                      x_execution_key: str | None = Header(default=None, max_length=200),
                      x_session_id: str | None = Header(default=None, max_length=100)) -> dict:
    rate_limits.enforce("write", http, identity=user.user_id)
    if user.kind == "member":
        idle_reset.touched(changed=True)
    payload = _record_payload(issue)
    if x_execution_key:
        # A whole ticket from the browser's own copy cannot match what a key bound (5b plan,
        # section 7.2); assistant changes use PATCH with exactly the bound change.
        raise HTTPException(status_code=400, detail="Assistant changes use PATCH with the bound change.")
    grant = require_record_access(user)
    records = _product_data(grant)
    existing = records.get_issue(issue_id)
    if existing is None:
        raise RecordNotFound("This ticket no longer exists.")
    # The user must be able to see the ticket now and wherever the edit moves it.
    require_any_scope(records.scopes_for_record(existing["projectId"], existing["project"]), grant)
    require_any_scope(records.scopes_for_record(issue.projectId, issue.project), grant)
    return records.update_issue(issue_id, payload)


@app.patch("/api/demo-data/issues/{issue_id}")
def apply_keyed_issue_change(issue_id: str, change: KeyedIssueUpdate,
                             http: Request,
                             user: AuthUser = Depends(require_auth),
                             idempotency_key: str | None = Header(default=None, max_length=200),
                             x_execution_key: str = Header(max_length=200),
                             x_session_id: str = Header(max_length=100)):
    """An assistant update: exactly the change its execution key bound (5b plan, section 7.2)."""
    rate_limits.enforce("write", http, identity=user.user_id)
    if user.kind == "member":
        idle_reset.touched(changed=True)
    _refuse_legacy_idempotency(idempotency_key)
    changes = dict(change.changes)
    return _keyed(x_execution_key, x_session_id, user,
                  {"action": "update_issue", "target": issue_id, "changes": changes},
                  lambda product_id: _update_issue(issue_id, changes, user, product_id))


def _validated(model: type[BaseModel], body: dict) -> BaseModel:
    try:
        return model.model_validate(body)
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from error


_ISSUE_FIELDS = frozenset(IssueInput.model_fields)


def _create_issue(fields: dict, user: AuthUser, product_id: str):
    """The create a key authorizes, inside the keyed write's transaction, in the key's workspace."""
    def apply(connection, grant: RecordGrant, scope_id: str) -> RecordChange:
        if user.kind == "member":
            _require_record_owner(connection, user, product_id)
        records = _product_data(grant)
        visible = records.load(frozenset({scope_id}), connection=connection)
        wanted = fields.get("project")
        # The project comes from the key's workspace only: one of its project records (by ID or
        # name), or one of the issue projects it allows. Nothing is guessed.
        project = next((row for row in visible["projects"] if wanted in (row["id"], row["name"])), None)
        allowed_names = {name for scope in visible["workspaceScopes"] for name in scope["allowedIssueProjects"]}
        if project is not None:
            placement = {"project": project["name"], "projectId": project["id"]}
        elif wanted in allowed_names:
            placement = {"project": wanted, "projectId": None}
        else:
            raise InvalidChange("A new ticket needs a project in this workspace.")
        issue = _record_payload(IssueInput.model_validate({
            **{name: value for name, value in fields.items() if name != "project"}, **placement,
        }))
        record = records.save_issue(issue, None, connection=connection)
        return RecordChange(record["id"], record)

    return apply


def _update_issue(issue_id: str, changes: dict, user: AuthUser, product_id: str):
    """The change an update key authorizes, applied to the record as it is now, in the key's workspace."""
    def apply(connection, grant: RecordGrant, scope_id: str) -> RecordChange:
        if user.kind == "member":
            _require_record_owner(connection, user, product_id)
        records = _product_data(grant)
        existing = records.get_issue(issue_id, connection=connection)
        if existing is None:
            raise RecordNotFound("This ticket no longer exists.")
        if scope_id not in records.scopes_for_record(existing["projectId"], existing["project"],
                                                     connection=connection):
            raise ScopeViolation("This ticket is not in the workspace this change was made for.")
        # The revision comes from the stored record, never from the browser's copy.
        merged = {name: value for name, value in {**existing, **changes}.items() if name in _ISSUE_FIELDS}
        issue = _record_payload(IssueInput.model_validate(merged))
        issue["revision"] = existing.get("revision")
        if issue["revision"] is None:
            issue.pop("revision")
        record = records.update_issue(issue_id, issue, connection=connection)
        return RecordChange(issue_id, record)

    return apply


def _reload_issue(connection, grant: RecordGrant, scope_id: str, issue_id: str) -> dict | None:
    """Read one ticket for a replay, under the caller's access now and inside the key's workspace."""
    if not grant.may_use(scope_id):
        return None
    records = _product_data(grant)
    record = records.get_issue(issue_id, connection=connection)
    if record is None:
        return None
    scopes = records.scopes_for_record(record["projectId"], record["project"], connection=connection)
    return record if scope_id in scopes else None


def _refuse_legacy_idempotency(idempotency_key: str | None) -> None:
    """A keyed write must not also carry the legacy retry header.

    The header selects a stored receipt inside the record store, which would let a dispatched
    action settle as `executed` while nothing was created. One action, one key, one outcome.
    """
    if idempotency_key:
        raise HTTPException(
            status_code=400,
            detail="An execution key is the only idempotency mechanism for an assistant write.",
        )


def _committed_values(changes: dict | None, record: dict | None) -> dict | None:
    """What the record now shows for each field it changed, rather than the value sent.

    A reference is stored as an identifier ("PRJ-102") and shown as a name ("Issue Triage
    Workflow"). The receipt says what a visitor can see, and identifiers are never spoken.
    """
    if not changes or not record:
        return changes
    return {name: record.get(name, value) for name, value in changes.items()}


def _require_record_owner(connection, user: AuthUser, product_id: str) -> None:
    """Inside the transaction: these records must still belong to this organization's product."""
    owner = legacy_record_owner(connection)
    if owner is None or (owner.tenant_id, owner.product_id) != (user.tenant_id, product_id):
        raise ExecutionRefused("record_access_withdrawn", conflict=False)


def _keyed(execution_key: str, session_id: str | None, user: AuthUser, change_set: dict, build,
           *, created: bool = False) -> JSONResponse:
    """Perform a record write under a one-time execution key and answer with its receipt.

    The write lands in the caller's own store: a visitor's private instance, or the member demo
    for a member (5b plan, section 7.3). Nothing here creates reference data.
    """
    if user.kind == "visitor":
        product_id = user.product_id or ""
    else:
        owner = legacy_record_owner()
        if owner is None or owner.tenant_id != user.tenant_id:
            raise ExecutionRefused("record_access_withdrawn", conflict=False)
        product_id = owner.product_id
    result = keyed_writes.write(
        build(product_id), execution_key=execution_key, principal=user, product_id=product_id,
        session_id=session_id, change_set=change_set, reload=_reload_issue,
    )
    executed = result.outcome == "executed"
    receipt = ExecutionReceipt(
        outcome=result.outcome,
        code=result.code,
        speech=receipt_speech(
            result.code, executed=executed, replay=result.replay, created=created,
            record_id=result.record_id,
            changes=_committed_values(change_set.get("changes") or change_set.get("fields"), result.record),
        ),
        record=result.record,
    )
    pin = agent.sessions.pin_for(session_id) if session_id else None
    outcome = "replayed" if result.replay else result.outcome
    telemetry = BackgroundTask(
        turn_telemetry.record, deployment_id=deployment_id(), tenant_id=user.tenant_id or "",
        product_id=product_id, definition_version=pin.definition_version if pin else None,
        authority=engine_mode(), counts={"receipt": outcome, "receipt_code": result.code},
    )
    return JSONResponse(status_code=200 if executed else 409, content=receipt.model_dump(mode="json"),
                        background=telemetry)


@app.post("/api/demo-data/projects")
def create_demo_project(project: ProjectInput, workspace_scope_id: str,
                        http: Request,
                        user: AuthUser = Depends(require_auth),
                        grant: RecordGrant = Depends(require_record_access),
                        idempotency_key: str | None = Header(default=None, max_length=200)) -> dict:
    rate_limits.enforce("write", http, identity=user.user_id)
    if user.kind == "member":
        idle_reset.touched(changed=True)
    require_scope(workspace_scope_id, grant)
    return _product_data(grant).save_project(
        _record_payload(project), workspace_scope_id, idempotency_key
    )


@app.post("/api/demo-data/cycles")
def create_demo_cycle(cycle: CycleInput,
                      http: Request,
                      user: AuthUser = Depends(require_auth),
                      grant: RecordGrant = Depends(require_record_access),
                      idempotency_key: str | None = Header(default=None, max_length=200)) -> dict:
    rate_limits.enforce("write", http, identity=user.user_id)
    if user.kind == "member":
        idle_reset.touched(changed=True)
    # Cycles without a project are workspace-wide and reserved for record administrators.
    if cycle.projectId is None:
        require_record_admin(grant)
    else:
        require_any_scope(_product_data(grant).scopes_for_record(cycle.projectId), grant)
    return _product_data(grant).save_cycle(_record_payload(cycle), idempotency_key)


@app.post("/api/demo-data/team-members")
def create_demo_team_member(member: MemberInput, workspace_scope_id: str,
                            http: Request,
                            user: AuthUser = Depends(require_auth),
                            grant: RecordGrant = Depends(require_record_access),
                            idempotency_key: str | None = Header(default=None, max_length=200)) -> dict:
    rate_limits.enforce("write", http, identity=user.user_id)
    if user.kind == "member":
        idle_reset.touched(changed=True)
    require_scope(workspace_scope_id, grant)
    return _product_data(grant).save_team_member(
        _record_payload(member), workspace_scope_id, idempotency_key
    )


def _record_payload(model: BaseModel) -> dict:
    """Keep the additive private revision out of unchanged shared-record responses."""
    payload = model.model_dump(mode="json")
    if payload.get("revision") is None:
        payload.pop("revision", None)
    return payload


# --- Paid-provider capabilities (the API is the only component that calls providers) ---


class SpeechRequest(BaseModel):
    model_config = {"extra": "forbid"}

    text: str = Field(min_length=1, max_length=2000)
    product_id: str = Field(min_length=1, max_length=64)
    session_id: str | None = Field(default=None, max_length=100)

    @field_validator("text")
    @classmethod
    def strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("text cannot be blank")
        return stripped


@app.post("/api/speech", response_class=Response)
def synthesize_speech(body: SpeechRequest, http: Request,
                      user: AuthUser = Depends(require_auth)) -> Response:
    """Every check runs before any budget is reserved or any provider is contacted."""
    rate_limits.enforce("speech", http, identity=user.user_id)
    if user.kind == "member":
        idle_reset.touched()
    try:
        access = authorize_product(user, body.product_id, agent.directory)
    except AccessDenied:
        raise HTTPException(status_code=404, detail="This product is not available.")
    try:
        definition_id = _speech_definition(user, access, body.session_id)
        speech_pin = agent.sessions.pin_for(body.session_id) if body.session_id else None
        speech_version = speech_pin.definition_version if speech_pin else access.binding.definition_version
        voice_style = agent.directory.definitions.load(definition_id, speech_version).definition.identity.voice_style
        speech = speech_service.synthesize(
            tenant=access.context,
            voice_style=voice_style,
            user_id=user.user_id,
            session_id=body.session_id,
            text=body.text,
        )
    except SpeechUnavailable as unavailable:
        return JSONResponse(
            status_code=unavailable.status_code,
            content={"detail": "Neural voice is unavailable right now.", "reason": unavailable.reason},
        )
    headers = {"Cache-Control": "private, no-store", "X-TTS-Engine": speech.engine}
    if speech.voice:
        headers["X-TTS-Voice"] = speech.voice
    return Response(content=speech.audio, media_type=speech.media_type, headers=headers)


def _speech_definition(user: AuthUser, access: ProductAccess, session_id: str | None) -> str:
    """The definition speech runs on, after the same lifecycle checks a chat turn applies.

    With a session, it must belong to the caller and to this product, and still be valid on its
    pinned definition: a retired version still serves its sessions, a revoked one does not.
    Without a session, the current definition of the product must be startable. A supplied
    session is never silently ignored.
    """
    if session_id is None:
        try:
            return pin_new_session(access, agent.directory.definitions).definition_id
        except DefinitionUnavailable as unavailable:
            raise SpeechUnavailable(409, unavailable.reason) from unavailable
    pin = agent.sessions.pin_for(session_id)
    if not agent.sessions.owns_session(
        session_id, user.user_id, user.tenant_id, demo_context=user.demo_context
    ) or (
        pin is not None and pin.product_id != access.binding.product_id
    ):
        raise HTTPException(status_code=404, detail="This conversation was not found.")
    try:
        return check_pinned_session(pin, agent.directory).definition_id
    except SessionEnded as ended:
        raise SpeechUnavailable(409, ended.reason) from ended


@app.get("/api/usage/summary")
def usage_summary(day: str | None = None, user: AuthUser = Depends(require_member)) -> dict:
    """Usage for the caller's organization, by owning team and product. Organization admins only."""
    require_org_admin(user)
    return {
        "tenant_id": user.tenant_id,
        "deployment_id": deployment_id(),
        "rows": usage.organization_summary(user.tenant_id, deployment_id(), day),
    }


@app.post("/api/turn", response_model=TurnResponse)
def create_turn(request: TurnRequest, http: Request, background: BackgroundTasks,
                user: AuthUser = Depends(require_auth), engine=Depends(turn_engine)) -> TurnResponse:
    started = time.monotonic()
    response = _answer_turn(request, http, background, user, engine)
    response = _or_platform_navigation(request, http, background, user, engine, response)
    # Metadata only, after the response (5c plan, section 10).
    background.add_task(_record_turn, request, user, response, (time.monotonic() - started) * 1000)
    return response


def _record_turn(request: TurnRequest, user: AuthUser, response: TurnResponse, milliseconds: float) -> None:
    pin = agent.sessions.pin_for(request.session_id)
    authority = engine_mode()
    counts = {
        "status": response.status,
        "stage": _turn_stage(authority, response),
        "dispatch": "keyed" if response.execution is not None else "none",
        "latency": turn_telemetry.latency_band(milliseconds),
        # Which workflow the turn exercised, as the action's own name: metadata, never its payload.
        # The acceptance report reads this to prove workflow coverage (5d plan, section 2.1a).
        "action": turn_telemetry.action_name(
            response.validated_action.type if response.validated_action else None
        ),
    }
    if response._model_outcome is not None:
        counts["model"] = response._model_outcome
    turn_telemetry.record(
        deployment_id=deployment_id(), tenant_id=user.tenant_id or "", product_id=request.product_id,
        definition_version=pin.definition_version if pin else None, authority=authority, counts=counts,
    )


# The one stage that means "this product could not help": an answer, a refusal, a question back
# or a quoted document are all answers, and none of them is a request about the application.
FELL_BACK = FALLBACK_STAGE
# A turn answered by the application rather than the product keeps its own memory, so a product's
# conversation is never joined to the application's.
PLATFORM_SESSION = "::platform"


def _or_platform_navigation(request: TurnRequest, http: Request, background: BackgroundTasks,
                            user: AuthUser, engine, answered: TurnResponse) -> TurnResponse:
    """Answer "take me back to my products" from inside a product.

    Asking to leave where you are is navigation, not a question the product should have to
    answer. Only a turn the product itself could make nothing of is offered to the application,
    and only when this deployment has an application definition and this caller may use it. The
    product's own conversation is untouched: the application answers in a session of its own.
    """
    console = configured_console()
    if (console is None or user.kind != "member"
            or request.product_id == console.product_id
            or answered.status != "completed"
            or answered.intent_trace.current_intent != FELL_BACK):
        return answered
    if agent.directory.product(user.tenant_id or "", console.product_id) is None:
        return answered
    moved = request.model_copy(update={
        "product_id": console.product_id,
        "session_id": f"{request.session_id}{PLATFORM_SESSION}",
        "workspace_scope_id": PRIMARY,
        "selected_issue_id": None,
        # The screen open is one of the product's, which the application has never heard of; sent
        # along, it made the application refuse and the product's "not sure" was all anyone heard.
        "current_page": None,
    })
    try:
        elsewhere = _answer_turn(moved, http, background, user, engine)
    except HTTPException:
        # No access to the application's own product is not a reason to lose the product's reply.
        return answered
    if elsewhere.validated_action is None or elsewhere.status != "completed":
        return answered
    # The conversation the caller is having is still the product's, so the turn is reported under
    # the session they are in.
    return elsewhere.model_copy(update={"session_id": request.session_id})


def _turn_stage(authority: str, response: TurnResponse) -> str:
    if authority == ENGINE_DEFINITION and response.intent_trace.current_intent:
        return response.intent_trace.current_intent
    if response.status in {"denied", "stale", "cancelled"}:
        return response.status
    action = response.validated_action
    if action is None:
        return "answer"
    return "proposed" if action.type in {"CREATE_DEMO_ISSUE", "UPDATE_DEMO_ISSUE"} else "action"


def _answer_turn(request: TurnRequest, http: Request, background: BackgroundTasks, user: AuthUser,
                 engine) -> TurnResponse:
    rate_limits.enforce("turn", http, identity=user.user_id)
    retention.schedule(background.add_task)
    if user.kind == "member":
        idle_reset.touched()
    try:
        authorize_product(user, request.product_id, agent.directory)
    except AccessDenied:
        # The agent answers with the same denial every unusable product gets.
        return agent.handle_turn(request, user)
    grant = product_record_grant(user, request.product_id)
    require_scope(request.workspace_scope_id, grant)
    # One materialized snapshot drives both language understanding and action validation. A
    # visitor's turn can never consult the shared member demo or another visitor's records.
    visible_data = _product_data(grant).load(grant.visible_scope_ids())
    if engine is not live_turn:
        return engine(request, user, visible_data, grant)
    # The previous engine sees only the selected workspace, so a person or record in another one
    # reads exactly like an unknown one (5c plan, section 3.3): rollback never restores that disclosure.
    selected_data = _product_data(grant).load(frozenset({request.workspace_scope_id}))
    if not shadow_switch_on():
        note_shadow_off()
        return live_turn(request, user, selected_data, grant)
    # Shadow mode: the new engine answers the same turn on the same records, and nothing it does
    # reaches the visitor or the database. Only bounded preparation and a non-blocking hand-off run
    # here; the comparison runs on the shadow worker. See app/services/shadow.py.
    shadow = shadow_controller()
    admitted = shadow.begin(user, grant, request, visible_data)
    try:
        response = live_turn(request, user, selected_data, grant)
    except BaseException:
        shadow.abandon(admitted)
        raise
    shadow.finish(admitted, request, response)
    shadow.counters.schedule(background.add_task)
    return response


@app.post("/api/conversations/{session_id}/close", status_code=200)
def close_conversation(session_id: str, user: AuthUser = Depends(require_auth)) -> dict:
    """End a conversation and withdraw anything it proposed but nobody carried out.

    Switching product, or leaving one, ends that conversation. A key it handed out and nobody
    used is withdrawn here rather than left to expire, so a proposal made about one product can
    never be presented after somebody has moved on from it.
    """
    if not agent.sessions.owns_session(
        session_id, user.user_id, user.tenant_id, demo_context=user.demo_context
    ):
        raise HTTPException(status_code=404, detail="This conversation was not found.")
    pin = agent.sessions.pin_for(session_id)
    owner = principal_owner(user, pin.product_id) if pin is not None else None
    withdrawn = 0
    if owner is not None:
        with get_connection() as connection:
            connection.execute("begin immediate")
            withdrawn = ExecutionLedger().cancel_session(connection, owner, session_id)
    return {"session_id": session_id, "withdrawn": withdrawn}


@app.post("/api/turn/{turn_id}/cancel", response_model=CancelTurnResponse)
def cancel_turn(turn_id: int, request: CancelTurnRequest,
                user: AuthUser = Depends(require_auth)) -> CancelTurnResponse:
    if not agent.sessions.owns_session(
        request.session_id, user.user_id, user.tenant_id, demo_context=user.demo_context
    ):
        raise HTTPException(status_code=404, detail="This conversation was not found.")
    pin = agent.sessions.pin_for(request.session_id)
    owner = principal_owner(user, pin.product_id) if pin is not None else None
    cancelled = agent.cancel_turn(request.session_id, turn_id, owner)
    return CancelTurnResponse(
        session_id=request.session_id,
        turn_id=turn_id,
        status="cancelled" if cancelled else "not_active",
    )
