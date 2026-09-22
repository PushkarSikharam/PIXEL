"""The keyed write path (3.2 plan, section 5.2; 5b plan, sections 6.2, 7 and 8).

An assistant mutation is executed by the client, never by the platform itself. The client presents
the one-time execution key it was given, with the exact change that key bound, and this module
performs the write so that everything shares a single `begin immediate` transaction:

1. the access, product, definition and session re-checks (`ExecutionGuard`);
2. the owner-bound claim on the key, which decides replay, cancellation, expiry and mismatch;
3. the workspace checks: the key's workspace must still be the caller's, and the record inside it;
4. the record change itself, in the caller's own store;
5. the outcome, with its result code, written back onto the key row.

The deterministic rule failures in `RULE_REJECTIONS` are outcomes: they commit as `failed` with a
stable code. Anything else (a database error, a bug) escapes, the transaction rolls back, and the
key stays `dispatched`, so the same key can be retried.

The ledger keeps only the changed record's ID, so a replay reloads that record through the
caller's own access instead of returning a stored copy.

Writes without a key are unchanged (parent plan 5.4): they remain guarded by the endpoint's own
authorization and are simply not covered by the execution guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import ValidationError

from app.db import get_connection
from app.definitions.access import Principal
from app.engine.actions import ActionState
from app.engine.execution import (
    APPLIED,
    INVALID_CHANGE,
    RECORD_CONFLICT,
    RECORD_NOT_FOUND,
    SCOPE_MISMATCH,
    SCOPE_UNAVAILABLE,
    Claim,
    ExecutionLedger,
    Outcome,
    principal_owner,
)
from app.record_access import RecordGrant
from app.services.execution_guard import ExecutionGuard
from app.services.product_data_store import (
    InvalidReference,
    RecordConflict,
    RecordNotFound,
    ScopeViolation,
)


class InvalidChange(ValueError):
    """The bound change is not valid for the record as it is now."""


# Deterministic rule failures commit as `failed` with these codes. Nothing else is caught.
RULE_REJECTIONS: dict[type[Exception], str] = {
    RecordNotFound: RECORD_NOT_FOUND,
    ScopeViolation: SCOPE_MISMATCH,
    RecordConflict: RECORD_CONFLICT,
    InvalidReference: INVALID_CHANGE,
    InvalidChange: INVALID_CHANGE,
    ValidationError: INVALID_CHANGE,
}


def rejection_code(error: Exception) -> str:
    return next((code for kind, code in RULE_REJECTIONS.items() if isinstance(error, kind)), INVALID_CHANGE)


@dataclass(frozen=True)
class RecordChange:
    """What a keyed write did: an identifier for the ledger, content for this reply."""

    record_id: str
    record: dict[str, Any]


@dataclass(frozen=True)
class WriteOutcome:
    """One recognized keyed-write outcome, from which the receipt is composed."""

    outcome: str  # "executed", "failed" or "refused"
    code: str
    record: dict[str, Any] | None
    record_id: str | None
    replay: bool
    claim: Claim


class KeyedWriter:
    """Performs one keyed record write. The caller supplies the change; this owns the boundary."""

    def __init__(
        self, ledger: ExecutionLedger | None = None, guard: ExecutionGuard | None = None,
    ) -> None:
        self._ledger = ledger or ExecutionLedger()
        self._guard = guard or ExecutionGuard()

    def write(
        self, apply: Callable[[Any, RecordGrant, str], RecordChange], *, execution_key: str,
        principal: Principal, product_id: str, session_id: str | None, change_set: Any,
        reload: Callable[[Any, RecordGrant, str, str], dict | None],
    ) -> WriteOutcome:
        """Run `apply(connection, grant, scope_id)` under the key and return the committed outcome.

        Raises `ExecutionRefused` (no receipt) when the caller's standing changed or the key is not
        recognized for them. `reload(connection, grant, scope_id, record_id)` serves a replay.
        """
        owner = principal_owner(principal, product_id)
        with get_connection() as connection:
            connection.execute("begin immediate")
            authority = self._guard.recheck(
                connection, principal, product_id=product_id, session_id=session_id
            )
            claim = self._ledger.claim(connection, execution_key, owner, request=change_set,
                                       session_id=session_id)
            if not claim.proceed:
                return self._settled(connection, claim, authority.grant, reload)
            if not authority.grant.may_use(claim.scope_id):
                self._ledger.settle(connection, execution_key, Outcome.FAILED, result_code=SCOPE_UNAVAILABLE)
                return WriteOutcome("failed", SCOPE_UNAVAILABLE, None, None, False, claim)
            try:
                change = apply(connection, authority.grant, claim.scope_id)
            except tuple(RULE_REJECTIONS) as error:
                code = rejection_code(error)
                self._ledger.settle(connection, execution_key, Outcome.FAILED, result_code=code)
                return WriteOutcome("failed", code, None, None, False, claim)
            self._ledger.settle(connection, execution_key, Outcome.EXECUTED, result_code=APPLIED,
                                record_id=change.record_id)
            return WriteOutcome("executed", APPLIED, change.record, change.record_id, False, claim)

    @staticmethod
    def _settled(connection, claim: Claim, grant: RecordGrant, reload) -> WriteOutcome:
        if claim.state == ActionState.EXECUTED:
            record = reload(connection, grant, claim.scope_id, claim.record_id or "")
            if record is None:
                # The change did happen; its record is not available to this caller now.
                return WriteOutcome("executed", "execution_result_unavailable", None, claim.record_id,
                                    True, claim)
            return WriteOutcome("executed", APPLIED, record, claim.record_id, True, claim)
        outcome = "failed" if claim.state == ActionState.FAILED else "refused"
        return WriteOutcome(outcome, claim.code or INVALID_CHANGE, None, None, claim.replay, claim)


def require_visible_scope(scope_ids: set[str], grant: RecordGrant) -> None:
    """Inside a keyed write, a scope refusal is a rule rejection, not an HTTP concern."""
    if not grant.may_use_any(scope_ids):
        raise ScopeViolation("You do not have access to this workspace.")
