from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
import logging
import threading
import time

from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, ValidationError, field_validator

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
from app.record_access import RecordGrant, legacy_record_owner
from app.services.engine_state import EngineStateStore
from app.services.legacy_adapter import dispatch_legacy_mutation
from app.schemas import CancelTurnRequest, CancelTurnResponse, ExecutionReceipt, TurnRequest, TurnResponse
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
    try:
        ExecutionLedger.prune()
    except Exception as error:  # noqa: BLE001 - retried at the next trigger
        logger.warning("execution_prune_failed", extra={"error": type(error).__name__})


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


def engine_mode() -> str:
    configured = (env_value("PIXEL_ENGINE_MODE") or ENGINE_LEGACY).strip().lower()
    return configured or ENGINE_LEGACY


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
    global _process_started
    _process_started = datetime.now(UTC)
    was_armed = rate_limits.armed
    reset_was_armed = idle_reset.armed
    migrate()
    # 5b: refuse to serve while a workspace-less key is still dispatched, before pruning can erase
    # the evidence (plan, section 7.1), then prune retained execution rows.
    require_no_legacy_keys()
    prune_execution_ledger()
    DemoInstanceStore().prune(limit=1000)
    rate_limits.arm()
    idle_reset.arm()
    try:
        if env_bool("PIXEL_DEMO_SEEDS", default=False):
            # Demo records are created once, at startup, and never by a request: a refused or
            # unauthorized request must leave product data exactly as it found it.
            product_data.seed_if_empty()
        yield
    finally:
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


@app.exception_handler(RecordConflict)
async def conflict_handler(_: Request, error: RecordConflict):
    return JSONResponse(status_code=409, content={"detail": str(error)})


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
    return {"status": "ok"}


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
            record_id=result.record_id, changes=change_set.get("changes") or change_set.get("fields"),
        ),
        record=result.record,
    )
    return JSONResponse(status_code=200 if executed else 409, content=receipt.model_dump(mode="json"))


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
        speech = speech_service.synthesize(
            tenant=access.context,
            voice_style=PRODUCTS_BY_ID[definition_id].voice_style,
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
