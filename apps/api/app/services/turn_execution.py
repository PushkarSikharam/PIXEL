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
5. finalize in one transaction (5c section 5, step 9): re-check that this is still the owner's
   active turn in this workspace and that the engine state is still at the loaded revision; then
   write the safe memory, the key for one dispatchable mutation, the messages, the signals and the
   compatibility projection, and complete the turn. If either check fails nothing is written and
   the turn is answered `stale`;
6. answer, with the execution envelope for a dispatched mutation.

A mutation that awaits confirmation carries no key and no executable action. A superseded turn is
answered `stale`, and nothing it proposed can be executed or remembered.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from app.db import get_connection
from app.definitions.access import AccessDenied, authorize_product
from app.definitions.organizations import OrganizationDirectory
from app.definitions.sessions import DefinitionUnavailable, SessionEnded, check_pinned_session, pin_new_session
from app.engine.conversation_engine import EngineTurn, TurnStage
from app.engine.actions import RecordRef
from app.engine.execution import ExecutionLedger, principal_owner
from app.engine.composer import ResponseComposer
from app.engine.memory import ConversationMemory, PendingConfirmation
from app.engine.model_turn import AwaitingConfirmation, Proposed, consider
from app.engine.prompt import PromptBuilder
from app.engine.provenance import TurnEvidence
from app.engine.router import IntentRouter, TurnContext
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
from app.services.model_gateway import ModelGateway
from app.services.session_manager import SessionManager

# Stages whose validated mutation may be executed on this turn; awaiting confirmation is not one.
EXECUTABLE_STAGES = frozenset({TurnStage.PROPOSED, TurnStage.WOULD_EXECUTE})
# Every engine stage's public status (5c plan, section 9). A stage missing here fails the turn
# loudly instead of being guessed; `completed` never means that a change was committed.
STAGE_STATUS: dict[TurnStage, str] = {
    TurnStage.PROPOSED: "completed",
    TurnStage.AWAITING_CONFIRMATION: "completed",
    TurnStage.WOULD_EXECUTE: "completed",
    TurnStage.REFUSED: "denied",
    TurnStage.CLARIFICATION: "completed",
    TurnStage.ANSWER: "completed",
    TurnStage.KNOWLEDGE: "completed",
    TurnStage.UNGROUNDED: "completed",
    TurnStage.CANCELLED: "completed",
    TurnStage.FALLBACK: "completed",
}


class NewEngineTurns:
    """Callable with the live engine's turn signature: (request, principal, visible_data, grant)."""

    def __init__(self, sessions: SessionManager, directory: OrganizationDirectory, package_for,
                 state: EngineStateStore | None = None, pending: PendingStateCache | None = None,
                 ledger: ExecutionLedger | None = None, gateway: ModelGateway | None = None) -> None:
        self._sessions = sessions
        self._directory = directory
        self._package_for = package_for
        self._state = state or EngineStateStore()
        # Full-fidelity memory for this process only; never a second durable copy (5c section 6.2).
        self._pending = pending or PendingStateCache()
        self._ledger = ledger or ExecutionLedger()
        # Off unless a deployment switches it on and supplies a transport (5c plan, section 8).
        self._gateway = gateway or ModelGateway()

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

        definition = self._directory.definitions.load(session_pin.definition_id,
                                                      session_pin.definition_version).definition
        prepared = prepare_turn(self._directory, self._package_for, principal, grant, request.product_id,
                                visible_data, scope_id, definition)
        if prepared is None:
            self._sessions.complete_turn(request.session_id, request.turn_id)
            return _denied(request, "no_engine_for_product")
        if reason := _invalid_context(request, definition, prepared):
            self._sessions.complete_turn(request.session_id, request.turn_id)
            return _denied(request, reason)
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
                selected=_selected_on(request, definition, prepared),
                last_change=self._ledger.last_executed(owner, request.session_id, scope_id),
                view=getattr(request, "current_page", None),
            ),
        )
        model_outcome = None
        if turn.stage == TurnStage.FALLBACK and self._gateway.enabled():
            turn, model_outcome = self._consult_model(turn, engine, translator, request, principal, session_pin)

        executable = turn.stage in EXECUTABLE_STAGES and turn.validated is not None and turn.legacy_action is not None
        change_set = None
        if executable and turn.validated.is_mutation:
            try:
                change_set = translator.change_set(turn.validated)
            except LookupError:
                # No keyed form for this mutation: nothing executes, and nothing is guessed.
                executable = False
        won, dispatched = self._finalize(owner, request, scope_id, engine_pin, turn, base_revision, change_set)
        if not won:
            return _stale(request)
        envelope = None
        if dispatched is not None:
            envelope = ExecutionEnvelope(
                key=dispatched.execution_key, session_id=request.session_id, turn_id=request.turn_id,
                expires_at=datetime.fromtimestamp(dispatched.expires_at, UTC).isoformat(),
            )
        response = _response(request, turn, envelope=envelope, executable=executable)
        response._model_outcome = model_outcome
        return response

    def _consult_model(self, turn: EngineTurn, engine, translator, request: TurnRequest, principal: Any,
                       session_pin) -> tuple[EngineTurn, str]:
        """One gateway attempt for a turn routing could not handle (5c plan, section 8).

        The reply passes the unchanged 4a chain. A non-mutating action is proposed; a mutation only
        ever awaits confirmation (the "yes" is resolved deterministically, with no provider call,
        against the next turn's fresh snapshot). The model never supplies speech, and any other
        outcome keeps the deterministic fallback.
        """
        definition, snapshot = engine.definition, engine.snapshot
        prompt = PromptBuilder(definition).build(snapshot, request.message)
        result = self._gateway.attempt(prompt.text, owner=session_pin.context, user_id=principal.user_id,
                                       session_id=request.session_id)
        if result.outcome != "reply":
            return turn, result.outcome
        message = IntentRouter(definition, snapshot).normalizer.normalize(request.message)
        outcome = consider(result.raw or "", definition=definition, snapshot=snapshot,
                           evidence=TurnEvidence(message=message, snapshot=snapshot))
        if not isinstance(outcome, (Proposed, AwaitingConfirmation)):
            return turn, "refused" if type(outcome).__name__ == "Refused" else "answered"
        try:
            legacy = translator.translate(outcome.validated)
        except LookupError:
            return turn, "untranslatable"
        composer = ResponseComposer(definition)
        action = outcome.validated.action
        if isinstance(outcome, Proposed):
            return replace(turn, stage=TurnStage.PROPOSED, validated=outcome.validated, legacy_action=legacy,
                           reply=composer.proposed(action)), "proposed"
        pending = PendingConfirmation(action, outcome.reason, request.turn_id)
        return replace(turn, stage=TurnStage.AWAITING_CONFIRMATION, validated=outcome.validated,
                       legacy_action=legacy, reply=composer.awaiting_confirmation(action),
                       memory=replace(turn.memory, pending_confirmation=pending)), "awaiting_confirmation"

    def _finalize(self, owner, request: TurnRequest, scope_id: str, engine_pin: EnginePin,
                  turn: EngineTurn, base_revision: int, change_set):
        """Finalize the turn in one transaction (5c plan, section 5, steps 9 and 10).

        Inside one `begin immediate`: this must still be the owner's active turn in this workspace,
        and the engine state must still be at the revision the turn loaded; then the safe memory,
        the execution key (for one dispatchable mutation), the messages, the signals and the
        compatibility projection are written, and the turn is completed. If either check fails,
        nothing from this turn is written and it is answered `stale`.

        Returns (won, dispatched key or None).
        """
        signals = [Signal(type=signal.type, value=signal.value, confidence=signal.confidence)
                   for signal in turn.signals]
        session_id, turn_id = request.session_id, request.turn_id
        try:
            with get_connection() as connection:
                connection.execute("begin immediate")
                if not self._ledger.turn_is_current(connection, owner, session_id=session_id,
                                                    turn_id=turn_id, scope_id=scope_id):
                    raise _LostRace
                new_revision = self._state.commit(
                    connection, owner, session_id, turn_id, scope_id, engine_pin, turn.memory,
                    expected_revision=base_revision,
                )
                if new_revision is None:
                    raise _LostRace
                dispatched = None
                if change_set is not None:
                    dispatched = self._ledger.dispatch(
                        turn.validated, owner, session_id=session_id, turn_id=turn_id, scope_id=scope_id,
                        change_set=change_set, connection=connection,
                    )
                self._sessions.store_message(session_id, turn_id, "user", request.message, scope_id,
                                             connection=connection)
                self._sessions.store_message(session_id, turn_id, "assistant", turn.reply.speech, scope_id,
                                             connection=connection)
                self._sessions.store_signals(session_id, turn_id, signals, scope_id, connection=connection)
                self._sessions.remember_session_context(session_id, signals, connection=connection)
                self._sessions.complete_turn(session_id, turn_id, connection=connection)
        except _LostRace:
            # Leaving the block without committing rolls every write of this turn back.
            return False, None
        # The in-process cache follows the committed revision only (5c plan, section 6.2).
        self._pending.put(owner, session_id, revision=new_revision, memory=turn.memory, history=turn.history)
        return True, dispatched


class _LostRace(Exception):
    """A newer turn, or a newer engine state, won; this turn writes nothing."""


def _response(request: TurnRequest, turn: EngineTurn, *, envelope: ExecutionEnvelope | None,
              executable: bool) -> TurnResponse:
    status = STAGE_STATUS[turn.stage]
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
        status=status,
        speech=turn.reply.speech,
        proposed_action=proposed,
        validated_action=validated,
        intent_trace=IntentTrace(
            role=profile.role, current_tool=profile.current_tool, goal=profile.goal,
            pain_point=profile.pain_point,
            relevant_feature=turn.feature.capitalize() if turn.feature else None,
            current_intent=str(turn.stage), reason=_trace_reason(turn),
            confidence=1.0 if turn.validated is not None else 0.5,
            status="denied" if status == "denied" else "active",
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


def _trace_reason(turn: EngineTurn) -> str:
    if turn.stage == TurnStage.REFUSED and turn.reply.template_key == "out_of_scope":
        return "Denied because the requested action is outside this product demo."
    if turn.stage == TurnStage.REFUSED and turn.reply.template_key == "broad_scope_refused":
        return "Denied because the request is outside the current workspace scope."
    if turn.stage == TurnStage.REFUSED and turn.reply.template_key == "destructive_refused":
        return "Denied because destructive actions are not available in this demo."
    if turn.stage == TurnStage.REFUSED and turn.reply.template_key:
        return f"Denied by {turn.reply.template_key}."
    return f"New engine: {turn.stage}."


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


def _invalid_context(request: TurnRequest, definition: Any, prepared) -> str | None:
    """Whether what this turn claims about the screen is something we must refuse to answer.

    Only two things are refused, and both mean the request is describing a place or a record that
    is not this caller's to describe: a screen this product does not have, and a record none of
    the records they can see has the identifier of.

    A record they *can* see, which simply is not what the screen they are on lists, is neither.
    It is a selection the browser kept after somebody moved: open one record, walk to a screen
    that lists a different kind, and the first is still selected. Refusing that ended the
    conversation - and because the page went on sending the same selection with every later
    message, it ended it permanently, with a sentence that explained nothing. It is dropped in
    `_selected_on` instead, and the turn is answered without it.
    """
    if request.current_page and definition.views.get(request.current_page) is None:
        return "invalid_turn_page"
    if not request.selected_issue_id:
        return None
    wanted = request.selected_issue_id.lower()
    visible = [record.id for records in prepared.records.values() for record in records
               if record.id.lower() == wanted]
    return "invalid_selected_record" if not visible else None


def _selected_on(request: TurnRequest, definition: Any, prepared) -> RecordRef | None:
    """The record this turn is about: the selected one, when the screen showing it lists that kind.

    A screen that lists nothing in particular carries whatever is selected. A screen that lists
    one kind of record carries a selection only of that kind, so a request to change "it" cannot
    quietly mean something of another kind that was opened several screens ago.
    """
    selected = selected_record(prepared.records, request.selected_issue_id)
    if selected is None:
        return None
    view = definition.views.get(request.current_page) if request.current_page else None
    if view is not None and view.entity is not None and selected.entity != view.entity:
        return None
    return selected
