"""The legacy authority adapter (5c plan, sections 3.3, 4 and 9).

While `PIXEL_ENGINE_MODE=legacy` (production today, and the rollback target after a cutover), the
old engine still decides a turn, but it no longer owns the write lifecycle. This module converts its
final create/update decision into exactly the same 5b execution key, change set and receipt path the
definition engine uses — never a keyless assistant write, and never a claim that a change applied
before it was actually dispatched.

It does not re-plan or re-validate anything: the old engine has already checked scope, visibility and
field shape. This only re-expresses that already-approved decision as the shared change-set contract
and asks the ledger to dispatch a key for it, exactly as if the new engine's translator had produced
it — because it is the same dict shape that translator produces from the same old-format payload.

A decision this adapter cannot honestly express as a keyed change (a type it does not recognise, or a
malformed payload) is refused, never dispatched and never spoken as if it applied (plan, section 2.3:
"An action that is untranslatable is never offered or dispatched").
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.definitions.organizations import OrganizationDirectory
from app.engine.actions import GenericAction, RecordRef, UnknownAction
from app.engine.execution import ExecutionLedger, ExecutionOwner, principal_owner
from app.engine.validator import ValidatedAction
from app.schemas import ExecutionEnvelope, TurnRequest, TurnResponse
from app.services.session_manager import SessionManager

_KEYED_TYPES = frozenset({"CREATE_DEMO_ISSUE", "UPDATE_DEMO_ISSUE"})
_REFUSAL_SPEECH = "I couldn't verify this change against your account, so nothing was applied."


def change_set_for(action_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """The keyed endpoint's change-set dict for an already-decided create/update payload.

    Mirrors the Linear translator's `change_set()` exactly, because it is deriving the identical
    dict from the identical old-format payload; `id` (a legacy-assigned identifier the keyed create
    endpoint never accepts, since the server always assigns it) is dropped rather than offered as a
    field.
    """
    fields = dict(payload)
    if action_type == "UPDATE_DEMO_ISSUE":
        target = fields.pop("issue_id", None)
        if not isinstance(target, str) or not fields:
            return None
        return {"action": "update_issue", "target": target, "changes": fields}
    if action_type == "CREATE_DEMO_ISSUE":
        fields.pop("id", None)
        if not fields:
            return None
        return {"action": "create_issue", "fields": fields}
    return None


def dispatch_legacy_mutation(response: TurnResponse, request: TurnRequest, user: Any, *,
                             sessions: SessionManager, directory: OrganizationDirectory,
                             ledger: ExecutionLedger | None = None) -> TurnResponse:
    """Attach a 5b execution envelope to a legacy create/update decision, or refuse it.

    Anything that is not a create/update decision is returned unchanged.
    """
    action = response.validated_action
    if action is None or action.type not in _KEYED_TYPES:
        return response
    change_set = change_set_for(action.type, dict(action.payload))
    if change_set is None:
        return _refused(response)

    pin = sessions.pin_for(request.session_id)
    if pin is None:
        return _refused(response)
    try:
        definition = directory.definitions.load(pin.definition_id, pin.definition_version).definition
        target = RecordRef("issue", change_set["target"]) if "target" in change_set else None
        fields = change_set.get("changes") or change_set.get("fields")
        generic = GenericAction.for_definition(
            definition, change_set["action"], target=target, fields=fields,
        )
    except (UnknownAction, KeyError):
        return _refused(response)
    validated = ValidatedAction(action=generic, definition_id=pin.definition_id,
                                definition_version=pin.definition_version)

    owner = principal_owner(user, request.product_id)
    # The legacy engine has already completed its own turn (cleared `active_turn_id`) by the time
    # this adapter runs, so supersession is checked against `latest_turn_id` instead.
    dispatched = (ledger or ExecutionLedger()).dispatch_if_current(
        validated, owner, session_id=request.session_id, turn_id=request.turn_id,
        scope_id=request.workspace_scope_id, change_set=change_set,
    )
    if dispatched is None:
        return _refused(response, superseded=True)
    envelope = ExecutionEnvelope(
        key=dispatched.execution_key, session_id=request.session_id, turn_id=request.turn_id,
        expires_at=datetime.fromtimestamp(dispatched.expires_at, UTC).isoformat(),
    )
    # The old engine's own wording claims the change already happened; a keyed mutation has not
    # happened yet, only been proposed and dispatched, so its speech is replaced rather than kept
    # (plan, section 11.4: a dishonest completion claim is an immediate rollback trigger).
    update: dict[str, Any] = {"execution": envelope, "speech": _proposed_speech(change_set)}
    if action.type == "CREATE_DEMO_ISSUE":
        # The client sends back exactly what the key is bound to; the legacy-assigned ID is not
        # part of it (the server assigns the ID when the change commits).
        update["validated_action"] = action.model_copy(update={"payload": dict(change_set["fields"])})
    return response.model_copy(update=update)


def _proposed_speech(change_set: dict[str, Any]) -> str:
    if change_set["action"] == "update_issue":
        changes = ", ".join(f"{field} to {value}" for field, value in change_set["changes"].items())
        return f"I'll update {change_set['target']}: {changes}."
    assignee = change_set["fields"].get("assignee")
    return f"I'll create a ticket{f' assigned to {assignee}' if assignee else ''}."


def _refused(response: TurnResponse, *, superseded: bool = False) -> TurnResponse:
    speech = "This turn was replaced by a newer request." if superseded else _REFUSAL_SPEECH
    status = "stale" if superseded else "denied"
    return response.model_copy(update={
        "status": status, "speech": speech, "validated_action": None, "execution": None,
    })
