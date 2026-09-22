"""The new engine answering a turn, with execution keys (5b plan, sections 6.1, 7 and 8) and
durable, restart-safe state (5c plan, section 6).

This is the 5b/5c authoritative path: `app.testing_main` makes it answer `/api/turn` for browser
tests, `app.main` makes it answer production traffic under `PIXEL_ENGINE_MODE=definition`, and API
tests call it directly.

One turn, in the plan's normative order:

1. authorize the product, create or verify the session, check its pin;
2. activate the turn: one transaction that also selects the workspace and supersedes older keys;
3. load durable engine state for this exact owner, session, pin and workspace (5c section 6); a
   process-local cache supplies full-fidelity pending state only while it still matches that state's
   revision, so a restart safely forgets an unanswered question rather than guessing at it;
4. run the pure engine on the caller's records in that workspace, with the owner- and
   workspace-bound ledger view for "what changed?";
5. dispatch a key only for a mutation that may be executed now, and only while this turn is still
   the session's active turn in this workspace (one transaction);
6. persist the safe subset of memory, the message/signal compatibility projection, and complete the
   turn; answer, with the execution envelope for a dispatched mutation.

A mutation that awaits confirmation carries no key and no executable action. A superseded turn is
answered `stale`, and nothing it proposed can be executed or remembered.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.db import get_connection
from app.definitions.access import AccessDenied, authorize_product
from app.definitions.organizations import OrganizationDirectory
from app.definitions.sessions import DefinitionUnavailable, SessionEnded, check_pinned_session, pin_new_session
from app.engine.conversation_engine import EngineTurn, TurnStage
from app.engine.execution import ExecutionLedger, principal_owner
from app.engine.memory import ConversationMemory
from app.engine.router import TurnContext
from app.engine.signals import SignalHistory
from app.schemas import (
    ExecutionEnvelope,
    IntentTrace,
    ProposedAction,
    RetrievedContext,
    SessionSummary,
    Signal,
    TurnRequest,
    TurnResponse,
    ValidatedAction as ValidatedActionPayload,
)
from app.services.engine_assembly import assemble_engine, prepare_turn, selected_record
from app.services.engine_state import EnginePin, EngineStateStore, PendingStateCache, rebuild_signal_history
from app.services.session_manager import SessionManager

# Stages whose validated mutation may be executed on this turn; awaiting confirmation is not one.
EXECUTABLE_STAGES = frozenset({TurnStage.PROPOSED, TurnStage.WOULD_EXECUTE})


class NewEngineTurns:
    """Callable with the live engine's turn signature: (request, principal, visible_data, grant)."""

    def __init__(self, sessions: SessionManager, directory: OrganizationDirectory, package_for,
                 state: EngineStateStore | None = None, pending: PendingStateCache | None = None,
                 ledger: ExecutionLedger | None = None) -> None:
        self._sessions = sessions
        self._directory = directory
        self._package_for = package_for
        self._state = state or EngineStateStore()
        # Full-fidelity memory for this process only; never a second durable copy (5c section 6.2).
        self._pending = pending or PendingStateCache()
        self._ledger = ledger or ExecutionLedger()

    def __call__(self, request: TurnRequest, principal: Any, visible_data: dict, grant: Any) -> TurnResponse:
        try:
            access = authorize_product(principal, request.product_id, self._directory)
        except AccessDenied as denied:
            return _denied(request, f"product_{denied.reason}")
        pin = None
        if not self._sessions.exists(request.session_id):
            try:
                pin = pin_new_session(access, self._directory.definitions)
            except DefinitionUnavailable as unavailable:
                return _denied(request, unavailable.reason)
        if not self._sessions.ensure_session(
            request.session_id, request.product_id, user_id=principal.user_id, tenant_id=principal.tenant_id,
            scope_id=request.workspace_scope_id, pin=pin, demo_context=getattr(principal, "demo_context", None),
        ):
            return _denied(request, "session_not_owned")
        try:
            session_pin = check_pinned_session(self._sessions.pin_for(request.session_id), self._directory)
        except SessionEnded as ended:
            return _denied(request, ended.reason)

        owner = principal_owner(principal, request.product_id)
        scope_id = request.workspace_scope_id
        if not self._sessions.activate_turn(request.session_id, request.turn_id, owner=owner, scope_id=scope_id):
            return _stale(request)

        prepared = prepare_turn(self._directory, self._package_for, principal, grant, request.product_id,
                                visible_data, scope_id)
        if prepared is None:
            self._sessions.complete_turn(request.session_id, request.turn_id)
            return _denied(request, "no_engine_for_product")
        definition = self._directory.definitions.load(session_pin.definition_id,
                                                      session_pin.definition_version).definition
        engine, translator = assemble_engine(prepared, definition, session_pin)

        engine_pin = EnginePin(session_pin.definition_id, session_pin.definition_version,
                               session_pin.definition_checksum, session_pin.knowledge_version)
        with get_connection() as connection:
            loaded = self._state.load(connection, owner, request.session_id, scope_id, engine_pin)
        if loaded is None:
            memory, history, base_revision = ConversationMemory(), SignalHistory(), 0
        else:
            cached = self._pending.get(owner, request.session_id, revision=loaded.revision)
            if cached is not None:
                memory, history = cached
            else:
                with get_connection() as connection:
                    history = rebuild_signal_history(connection, owner, request.session_id, scope_id)
                memory = loaded.memory
            base_revision = loaded.revision

        turn = engine.turn(
            request.message, memory, history,
            TurnContext(
                turn=request.turn_id,
                selected=selected_record(prepared.records, request.selected_issue_id),
                last_change=self._ledger.last_executed(owner, request.session_id, scope_id),
            ),
        )

        envelope = None
        executable = turn.stage in EXECUTABLE_STAGES and turn.validated is not None and turn.legacy_action is not None
        if executable and turn.validated.is_mutation:
            try:
                change_set = translator.change_set(turn.validated)
            except LookupError:
                # No keyed form for this mutation: nothing executes, and nothing is guessed.
                self._finalize(owner, request, scope_id, engine_pin, turn, base_revision)
                return _response(request, turn, envelope=None, executable=False)
            dispatched = self._ledger.dispatch_if_active(
                turn.validated, owner, session_id=request.session_id, turn_id=request.turn_id,
                scope_id=scope_id, change_set=change_set,
            )
            if dispatched is None:
                return _stale(request)
            envelope = ExecutionEnvelope(
                key=dispatched.execution_key, session_id=request.session_id, turn_id=request.turn_id,
                expires_at=datetime.fromtimestamp(dispatched.expires_at, UTC).isoformat(),
            )
        self._finalize(owner, request, scope_id, engine_pin, turn, base_revision)
        return _response(request, turn, envelope=envelope, executable=executable)

    def _finalize(self, owner, request: TurnRequest, scope_id: str, engine_pin: EnginePin,
                  turn: EngineTurn, base_revision: int) -> None:
        """Persist the safe memory subset and the message/signal compatibility projection.

        Never overwrites newer state: `commit` only applies where the loaded revision still matches
        and `last_turn < turn_id` (plan, section 6.3). A lost race here simply keeps the newer row;
        this turn's own reply was already decided and is still returned to the caller.
        """
        with get_connection() as connection:
            connection.execute("begin immediate")
            new_revision = self._state.commit(
                connection, owner, request.session_id, request.turn_id, scope_id, engine_pin,
                turn.memory, expected_revision=base_revision,
            )
        if new_revision is not None:
            self._pending.put(owner, request.session_id, revision=new_revision,
                              memory=turn.memory, history=turn.history)
        signals = [Signal(type=signal.type, value=signal.value, confidence=signal.confidence)
                   for signal in turn.signals]
        self._sessions.store_message(request.session_id, request.turn_id, "user", request.message, scope_id)
        self._sessions.store_message(request.session_id, request.turn_id, "assistant", turn.reply.speech, scope_id)
        self._sessions.store_signals(request.session_id, request.turn_id, signals, scope_id)
        self._sessions.remember_session_context(request.session_id, signals)
        self._sessions.complete_turn(request.session_id, request.turn_id)


def _response(request: TurnRequest, turn: EngineTurn, *, envelope: ExecutionEnvelope | None,
              executable: bool) -> TurnResponse:
    refused = turn.stage == TurnStage.REFUSED
    legacy = turn.legacy_action
    proposed = ProposedAction(type=legacy.type, payload=dict(legacy.payload)) if legacy is not None else None
    # Only an action that may run now is handed to the client; a mutation also needs its key.
    validated = None
    if legacy is not None and executable and (envelope is not None or not turn.validated.is_mutation):
        validated = ValidatedActionPayload(type=legacy.type, payload=dict(legacy.payload))
    profile = turn.profile
    summary = turn.summary
    return TurnResponse(
        session_id=request.session_id,
        turn_id=request.turn_id,
        status="denied" if refused else "completed",
        speech=turn.reply.speech,
        proposed_action=proposed,
        validated_action=validated,
        intent_trace=IntentTrace(
            role=profile.role, current_tool=profile.current_tool, goal=profile.goal,
            pain_point=profile.pain_point,
            relevant_feature=turn.feature.capitalize() if turn.feature else None,
            current_intent=str(turn.stage), reason=f"New engine: {turn.stage}.",
            confidence=1.0 if turn.validated is not None else 0.5,
            status="denied" if refused else "active",
        ),
        signals=[Signal(type=signal.type, value=signal.value, confidence=signal.confidence)
                 for signal in turn.signals],
        retrieved_context=[RetrievedContext(title=passage.title or "Product documentation", source=passage.source,
                                            snippet=passage.snippet) for passage in turn.passages],
        session_summary=SessionSummary(
            interests=list(summary.interests), pain_points=list(summary.pain_points),
            last_person=summary.last_person, last_feature=summary.last_feature,
            clarification_pending=summary.clarification_pending,
        ),
        execution=envelope,
    )


def _denied(request: TurnRequest, reason: str) -> TurnResponse:
    response = TurnResponse(
        session_id=request.session_id, turn_id=request.turn_id, status="denied",
        speech="This conversation cannot continue here.", proposed_action=None, validated_action=None,
        intent_trace=IntentTrace(status="denied", reason=f"Denied ({reason})."),
    )
    response._engine_entered = False
    return response


def _stale(request: TurnRequest) -> TurnResponse:
    return TurnResponse(
        session_id=request.session_id, turn_id=request.turn_id, status="stale",
        speech="This turn was replaced by a newer request.", proposed_action=None, validated_action=None,
        intent_trace=IntentTrace(status="interrupted", reason="Superseded by a newer turn."),
    )
