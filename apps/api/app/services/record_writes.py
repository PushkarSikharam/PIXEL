"""The keyed write path (3.2 plan, section 5.2).

A proposed action is executed by the client, never by the platform itself. The client presents
the one-time execution key it was given, and this module performs the write so that four things
share a single `begin immediate` transaction:

1. the access, product, definition and session re-checks (`ExecutionGuard`);
2. the claim on the key, which decides replay, cancellation and expiry;
3. the record change itself;
4. the outcome written back onto the key row.

Because they commit together, a cancellation and an execution race on one commit: whichever
commits first wins, and the loser sees the other's committed state. A rule rejection is a real
outcome, so it commits as `failed`. An unexpected error commits nothing: the transaction rolls
back and the key stays `dispatched`, which is the only state from which a retry is possible.

The ledger keeps only the changed record's ID, so a replay reloads that record through the
caller's own access instead of returning a stored copy. A record that is gone, or no longer
visible to this caller, therefore cannot be replayed, and says so.

Writes without a key are unchanged (section 5.4): they remain guarded by the endpoint's own
authorization and are simply not covered by the execution guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.db import get_connection
from app.definitions.access import Principal
from app.engine.execution import ExecutionLedger, ExecutionOwner, ExecutionRefused, Outcome
from app.record_access import RecordGrant
from app.services.execution_guard import ExecutionGuard
from app.services.product_data_store import (
    InvalidReference,
    RecordConflict,
    RecordNotFound,
    ScopeViolation,
)

# A rule rejection is a decision, not a failure of the mechanism: it commits as `failed`.
RULE_REJECTIONS: dict[type[Exception], str] = {
    RecordNotFound: "record_not_found",
    ScopeViolation: "scope_violation",
    InvalidReference: "invalid_reference",
    RecordConflict: "record_conflict",
}


def rejection_code(error: Exception) -> str:
    return next((code for kind, code in RULE_REJECTIONS.items() if isinstance(error, kind)), "rejected")


@dataclass(frozen=True)
class RecordChange:
    """What a keyed write did: an identifier and a code for the ledger, content for this reply."""

    record_id: str
    result_code: str
    record: dict[str, Any]


class KeyedWriter:
    """Performs one keyed record write. The caller supplies the change; this owns the boundary."""

    def __init__(
        self, ledger: ExecutionLedger | None = None, guard: ExecutionGuard | None = None,
    ) -> None:
        self._ledger = ledger or ExecutionLedger()
        self._guard = guard or ExecutionGuard()

    def write(
        self, apply: Callable[[Any, RecordGrant], RecordChange], *, execution_key: str,
        principal: Principal, product_id: str, session_id: str | None, request: Any,
        reload: Callable[[Any, RecordGrant, str], dict | None],
    ) -> dict:
        """Run `apply` under the key, or raise: `ExecutionRefused`, or the rule that rejected it.

        `reload` reads one record by ID under this caller's access, and serves a replay.
        """
        context = getattr(principal, "demo_context", None)
        owner = ExecutionOwner(
            principal.tenant_id, product_id, principal.user_id,
            context.instance_id if context else None,
            context.generation if context else None,
        )
        rejection: Exception | None = None
        change: RecordChange | None = None
        with get_connection() as connection:
            connection.execute("begin immediate")
            authority = self._guard.recheck(
                connection, principal, product_id=product_id, session_id=session_id
            )
            replay = self._ledger.claim(connection, execution_key, owner, request=request,
                                        session_id=session_id)
            if replay is not None:
                # Already settled: the same outcome is reported again, never repeated.
                if replay.state != Outcome.EXECUTED:
                    raise ExecutionRefused(replay.reason or "execution_failed")
                record = reload(connection, authority.grant, replay.record_id or "")
                if record is None:
                    # The change did happen; its record is not available to this caller now.
                    raise ExecutionRefused("execution_result_unavailable")
                return record
            try:
                change = apply(connection, authority.grant)
            except tuple(RULE_REJECTIONS) as error:
                rejection = error
                self._ledger.settle(
                    connection, execution_key, Outcome.FAILED, reason=rejection_code(error)
                )
            else:
                self._ledger.settle(
                    connection, execution_key, Outcome.EXECUTED,
                    record_id=change.record_id, result_code=change.result_code,
                )
        if rejection is not None:
            raise rejection
        return change.record if change else {}


def require_visible_scope(scope_ids: set[str], grant: RecordGrant) -> None:
    """Inside a keyed write, a scope refusal is a rule rejection, not an HTTP concern."""
    if not grant.may_use_any(scope_ids):
        raise ScopeViolation("You do not have access to this workspace.")
