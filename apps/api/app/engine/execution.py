"""The execution ledger (3.2 plan, section 5; 5b plan, sections 6 to 9).

The platform never writes records itself. When a validated mutation is dispatched to a client, it
is recorded here as `dispatched` with a one-time execution key, bound to its owner (organization,
product, user, private instance and generation), its session and turn, the workspace it was issued
in, and the digest of the exact change it authorizes. A keyed write is accepted only inside one
database transaction that re-checks access, claims the key, changes the record and commits the
outcome on the same row.

Every settled row carries a durable `result_code` (enforced by database triggers), so the outcome
a client is told is derived from committed state, never from an exception string:

| Event | Transition | Code |
| --- | --- | --- |
| record change commits | dispatched -> executed | `applied` |
| deterministic rule failure on an owned key | dispatched -> failed | specific code |
| request does not match the bound change | dispatched -> failed | `invalid_change` |
| a newer turn of the session activates | dispatched -> cancelled | `superseded` |
| the turn is explicitly cancelled | dispatched -> cancelled | `user_cancelled` |
| an expired key is claimed | dispatched -> cancelled | `expired` |
| the private demo is reset | dispatched -> cancelled | `instance_reset` |

Unknown keys, other owners' keys, keys from an earlier private generation and legacy keys without a
workspace are all refused alike, as not found, with no row changed.

**The ledger stores no customer data.** A row holds identifiers, a digest of the change and an
outcome code. A replay reloads the changed record under the caller's access at that moment.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from enum import StrEnum
from secrets import token_urlsafe
from typing import Any

from app.db import get_connection
from app.engine.actions import ActionState, GenericAction
from app.engine.validator import ValidatedAction
from app.tenancy import ProductContext

# An unused key stops working after this long, so a stale one cannot be replayed later.
KEY_LIFETIME_SECONDS = 600
# Settled and expired rows are kept this long, then pruned (5b plan, section 9).
RETENTION_SECONDS = 30 * 24 * 60 * 60
PRUNE_BATCH = 500
PRUNE_BATCHES = 20

APPLIED = "applied"
SUPERSEDED = "superseded"
USER_CANCELLED = "user_cancelled"
EXPIRED = "expired"
INSTANCE_RESET = "instance_reset"
INVALID_CHANGE = "invalid_change"
SCOPE_UNAVAILABLE = "scope_unavailable"
SCOPE_MISMATCH = "scope_mismatch"
RECORD_NOT_FOUND = "record_not_found"
RECORD_CONFLICT = "record_conflict"


class ExecutionRefused(Exception):
    """The keyed write must not happen, and no receipt is given; `reason` is a stable code.

    `not_found` is True when the key is not recognized for this caller (unknown, another owner,
    another session, an earlier instance generation, a legacy key): the answer reveals nothing.
    `conflict` distinguishes a key that cannot be used from a caller whose standing changed.
    """

    def __init__(self, reason: str, conflict: bool = True, *, not_found: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.conflict = conflict
        self.not_found = not_found


class Outcome(StrEnum):
    EXECUTED = ActionState.EXECUTED
    FAILED = ActionState.FAILED
    CANCELLED = ActionState.CANCELLED


@dataclass(frozen=True)
class ExecutionOwner:
    tenant_id: str
    product_id: str
    user_id: str
    instance_id: str | None = None
    instance_generation: int | None = None

    def values(self) -> tuple:
        return (self.tenant_id, self.product_id, self.user_id, self.instance_id, self.instance_generation)


# Null-safe owner match on `action_executions` (instance columns are null for members).
_OWNER_MATCH = ("tenant_id = ? and product_id = ? and user_id = ? "
                "and instance_id is ? and instance_generation is ?")


@dataclass(frozen=True)
class DispatchedAction:
    """What the client is told, in this response only. Field values are never stored."""

    execution_key: str
    action_key: str
    capability: str
    entity: str | None
    target_id: str | None
    fields: dict[str, Any]
    session_id: str = ""
    turn_id: int = 0
    expires_at: float = 0.0


@dataclass(frozen=True)
class ExecutedAction:
    """A change this session completed, as the ledger remembers it: identifiers, not content."""

    action_key: str
    capability: str
    entity: str | None
    record_id: str | None
    result_code: str | None


@dataclass(frozen=True)
class Claim:
    """The result of claiming an owned key inside the caller's transaction.

    `proceed` means the write may happen now. Otherwise `state` and `code` are the key's committed
    outcome (possibly committed by this claim: an expiry or a mismatched change), and `replay` says
    whether that outcome existed before this request.
    """

    proceed: bool
    state: ActionState
    code: str | None
    record_id: str | None
    replay: bool
    action_key: str
    capability: str
    entity: str | None
    target_id: str | None
    scope_id: str


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _fields_of(action: GenericAction) -> dict[str, Any]:
    return {name: value for name, value in sorted((action.fields or {}).items())}


def canonical(request: Any) -> str:
    """One stable text for a change set, so dispatch and the endpoint compare the same thing."""
    return json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)


def digest(request: Any) -> str:
    """What the ledger keeps of a change: enough to recognise it, nothing to disclose."""
    return hashlib.sha256(canonical(request).encode("utf-8")).hexdigest()


class ExecutionLedger:
    # --- dispatch ---

    def dispatch(
        self, validated: ValidatedAction, owner: ExecutionOwner, *, session_id: str, turn_id: int,
        scope_id: str, change_set: Any, connection=None,
    ) -> DispatchedAction:
        """Record a validated mutation as dispatched and return its one-time key.

        This is the insert only; request paths use `dispatch_if_active`, which first proves the
        turn is still the session's active turn in the same transaction.
        """
        if not scope_id:
            raise ValueError("an execution key is always bound to a workspace")
        action = validated.action
        key = token_urlsafe(24)
        expires_at = time.time() + KEY_LIFETIME_SECONDS
        row = (
            key, owner.tenant_id, owner.product_id, session_id, turn_id, owner.user_id,
            owner.instance_id, owner.instance_generation, scope_id,
            action.action_key, str(action.capability), action.target.entity if action.target else None,
            action.target.id if action.target else None, digest(change_set),
            ActionState.DISPATCHED.value, _now(), expires_at,
        )
        statement = """
            insert into action_executions(
              execution_key, tenant_id, product_id, session_id, turn_id, user_id,
              instance_id, instance_generation, scope_id, action_key,
              capability, entity, target_id, request_digest, state, created_at, expires_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        if connection is not None:
            connection.execute(statement, row)
        else:
            with get_connection() as own:
                own.execute(statement, row)
        return DispatchedAction(
            key, action.action_key, str(action.capability),
            action.target.entity if action.target else None,
            action.target.id if action.target else None, _fields_of(action),
            session_id, turn_id, expires_at,
        )

    def dispatch_if_active(
        self, validated: ValidatedAction, owner: ExecutionOwner, *, session_id: str, turn_id: int,
        scope_id: str, change_set: Any,
    ) -> DispatchedAction | None:
        """Dispatch only while this turn is still the owner's active turn in this workspace.

        One transaction: the owner, the active turn and the session's recorded workspace are read
        and the key inserted together, so a turn superseded meanwhile gets no key (5b plan,
        section 6.1 step 6).
        """
        with get_connection() as connection:
            connection.execute("begin immediate")
            row = connection.execute(
                """
                select s.active_turn_id, o.scope_id, o.user_id, o.customer_id, o.product_id,
                       o.instance_id, o.instance_generation
                from sessions s join conversation_owners o on o.session_id = s.id
                where s.id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None or (
                row["customer_id"], row["product_id"], row["user_id"],
                row["instance_id"], row["instance_generation"],
            ) != owner.values():
                return None
            if row["active_turn_id"] != turn_id or row["scope_id"] != scope_id:
                return None
            return self.dispatch(validated, owner, session_id=session_id, turn_id=turn_id,
                                 scope_id=scope_id, change_set=change_set, connection=connection)

    def dispatch_if_current(
        self, validated: ValidatedAction, owner: ExecutionOwner, *, session_id: str, turn_id: int,
        scope_id: str, change_set: Any,
    ) -> DispatchedAction | None:
        """Dispatch for a caller that has already cleared its own `active_turn_id` by the time it
        asks (5c plan, section 3.3: the legacy engine completes a turn before this adapter runs).

        `latest_turn_id` is set at activation and never cleared by completion, so it still answers
        "has a newer turn been activated since," which is the same supersession guarantee
        `dispatch_if_active` gives a caller that asks while its own turn is still active.
        """
        with get_connection() as connection:
            connection.execute("begin immediate")
            row = connection.execute(
                """
                select s.latest_turn_id, o.scope_id, o.user_id, o.customer_id, o.product_id,
                       o.instance_id, o.instance_generation
                from sessions s join conversation_owners o on o.session_id = s.id
                where s.id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None or (
                row["customer_id"], row["product_id"], row["user_id"],
                row["instance_id"], row["instance_generation"],
            ) != owner.values():
                return None
            if row["latest_turn_id"] != turn_id or row["scope_id"] != scope_id:
                return None
            return self.dispatch(validated, owner, session_id=session_id, turn_id=turn_id,
                                 scope_id=scope_id, change_set=change_set, connection=connection)

    # --- claim and settle ---

    def claim(
        self, connection, execution_key: str, owner: ExecutionOwner, *, request: Any,
        session_id: str | None,
    ) -> Claim:
        """Take the key for this write, inside the caller's `begin immediate` transaction.

        Raises `ExecutionRefused(not_found=True)` for any key this caller may not know about. For
        an owned key, returns a `Claim`; an expiry or a mismatched change is settled here, in the
        caller's transaction, so its outcome is durable before any answer is given.
        """
        row = connection.execute(
            "select * from action_executions where execution_key = ?", (execution_key,)
        ).fetchone()
        if row is None or session_id is None:
            raise ExecutionRefused("unknown_execution_key", not_found=True)
        owned = (
            row["tenant_id"], row["product_id"], row["user_id"],
            row["instance_id"], row["instance_generation"],
        ) == owner.values()
        if not owned or row["session_id"] != session_id or not row["scope_id"]:
            # Another owner's key, another conversation's key, a key from before a private reset
            # and a legacy key without a workspace are all the same unknown key.
            raise ExecutionRefused("unknown_execution_key", not_found=True)
        state = ActionState(row["state"])
        matches = digest(request) == row["request_digest"]
        details = dict(
            action_key=row["action_key"], capability=row["capability"], entity=row["entity"],
            target_id=row["target_id"], scope_id=row["scope_id"],
        )
        if state != ActionState.DISPATCHED:
            if not matches:
                raise ExecutionRefused("execution_request_mismatch")
            return Claim(False, state, row["result_code"], row["result_record_id"], True, **details)
        if row["expires_at"] < time.time():
            self.settle(connection, execution_key, Outcome.CANCELLED, result_code=EXPIRED)
            return Claim(False, ActionState.CANCELLED, EXPIRED, None, False, **details)
        if not matches:
            # A known, owned key cannot be probed or reused with another change.
            self.settle(connection, execution_key, Outcome.FAILED, result_code=INVALID_CHANGE)
            return Claim(False, ActionState.FAILED, INVALID_CHANGE, None, False, **details)
        return Claim(True, ActionState.DISPATCHED, None, None, False, **details)

    def settle(self, connection, execution_key: str, outcome: Outcome, *, result_code: str,
               record_id: str | None = None, reason: str | None = None) -> None:
        """Commit the outcome on the key row, in the same transaction as the record change."""
        if not result_code:
            raise ValueError("every settled execution has a result code")
        cursor = connection.execute(
            """
            update action_executions
            set state = ?, result_record_id = ?, result_code = ?, reason = ?, settled_at = ?
            where execution_key = ? and state = ?
            """,
            (
                outcome.value, record_id, result_code, reason, _now(),
                execution_key, ActionState.DISPATCHED.value,
            ),
        )
        if cursor.rowcount != 1:
            raise ExecutionRefused("execution_already_settled")

    # --- supersession and cancellation (always owner-bound) ---

    def supersede(self, connection, owner: ExecutionOwner, session_id: str, turn_id: int) -> int:
        """At a newer turn's activation: cancel this owner's unused keys from older turns."""
        return connection.execute(
            f"""
            update action_executions set state = ?, result_code = ?, settled_at = ?
            where session_id = ? and {_OWNER_MATCH} and state = ? and turn_id < ?
            """,
            (ActionState.CANCELLED.value, SUPERSEDED, _now(), session_id, *owner.values(),
             ActionState.DISPATCHED.value, turn_id),
        ).rowcount

    def cancel_turn(self, connection, owner: ExecutionOwner, session_id: str, turn_id: int) -> int:
        """An explicit cancel: this owner's unused keys of exactly that turn, in any workspace."""
        return connection.execute(
            f"""
            update action_executions set state = ?, result_code = ?, settled_at = ?
            where session_id = ? and {_OWNER_MATCH} and state = ? and turn_id = ?
            """,
            (ActionState.CANCELLED.value, USER_CANCELLED, _now(), session_id, *owner.values(),
             ActionState.DISPATCHED.value, turn_id),
        ).rowcount

    def cancel_instance(self, connection, owner: ExecutionOwner) -> int:
        """In the private-reset transaction: every unused key of the old generation."""
        return connection.execute(
            f"""
            update action_executions set state = ?, result_code = ?, reason = ?, settled_at = ?
            where {_OWNER_MATCH} and state = ?
            """,
            (ActionState.CANCELLED.value, INSTANCE_RESET, "demo_reset", _now(), *owner.values(),
             ActionState.DISPATCHED.value),
        ).rowcount

    # --- reads (owner- and workspace-bound) ---

    def last_executed(self, owner: ExecutionOwner, session_id: str, scope_id: str) -> ExecutedAction | None:
        """The most recent change this owner completed in this session and workspace.

        Nothing is read unless the session is this owner's. A session ID reused or guessed by
        anyone else, an earlier instance generation and another workspace all read nothing.
        """
        with get_connection() as connection:
            owner_row = connection.execute(
                "select user_id, customer_id, product_id, instance_id, instance_generation "
                "from conversation_owners where session_id = ?",
                (session_id,),
            ).fetchone()
            if owner_row is None or (
                owner_row["customer_id"], owner_row["product_id"], owner_row["user_id"],
                owner_row["instance_id"], owner_row["instance_generation"],
            ) != owner.values():
                return None
            row = connection.execute(
                f"""
                select * from action_executions
                where session_id = ? and {_OWNER_MATCH} and scope_id = ? and state = ?
                order by settled_at desc, rowid desc limit 1
                """,
                (session_id, *owner.values(), scope_id, ActionState.EXECUTED.value),
            ).fetchone()
        if row is None:
            return None
        return ExecutedAction(
            row["action_key"], row["capability"], row["entity"], row["result_record_id"],
            row["result_code"],
        )

    def state_of(self, execution_key: str) -> ActionState | None:
        """Operator and test evidence only; never reachable from a request."""
        with get_connection() as connection:
            row = connection.execute(
                "select state from action_executions where execution_key = ?", (execution_key,)
            ).fetchone()
        return ActionState(row["state"]) if row else None

    # --- deployment gate and retention ---

    @staticmethod
    def preflight(connection=None) -> dict[str, Any]:
        """Counts by state, and the legacy (workspace-less) keys that are still dispatched."""
        def read(conn) -> dict[str, Any]:
            counts = {row["state"]: row["count"] for row in conn.execute(
                "select state, count(*) as count from action_executions group by state"
            )}
            legacy = conn.execute(
                "select count(*) as count from action_executions where scope_id is null and state = ?",
                (ActionState.DISPATCHED.value,),
            ).fetchone()["count"]
            return {"states": counts, "legacy_dispatched": legacy}

        if connection is not None:
            return read(connection)
        with get_connection() as own:
            return read(own)

    @staticmethod
    def prune(now: float | None = None, *, batch: int = PRUNE_BATCH, batches: int = PRUNE_BATCHES) -> int:
        """Delete rows 30 days after they settled, or 30 days after an unused key expired.

        Bounded: at most `batches` batches of `batch` rows, oldest first, one short transaction
        per batch. Both queries use their partial indexes.
        """
        current = time.time() if now is None else now
        settled_before = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(current - RETENTION_SECONDS))
        expired_before = current - RETENTION_SECONDS
        removed = 0
        for statement, values in (
            ("delete from action_executions where rowid in (select rowid from action_executions "
             "where settled_at is not null and settled_at < ? order by settled_at limit ?)",
             (settled_before,)),
            ("delete from action_executions where rowid in (select rowid from action_executions "
             "where state = 'dispatched' and expires_at < ? order by expires_at limit ?)",
             (expired_before,)),
        ):
            for _ in range(batches):
                with get_connection() as connection:
                    count = connection.execute(statement, (*values, batch)).rowcount
                removed += count
                if count < batch:
                    break
        return removed


class LegacyKeysPresent(RuntimeError):
    """A workspace-less key is still dispatched; this build must not serve traffic over it."""


def require_no_legacy_keys() -> dict[str, Any]:
    """The mandatory startup gate (5b plan, section 7.1): run after migration, before retention."""
    report = ExecutionLedger.preflight()
    if report["legacy_dispatched"]:
        raise LegacyKeysPresent(
            f"{report['legacy_dispatched']} dispatched execution keys have no workspace; "
            "run `python -m app.ops execution-preflight` and resolve them before deploying"
        )
    return report


def principal_owner(principal: Any, product_id: str) -> ExecutionOwner:
    """The owner a principal's keys are bound to: organization, product, user and instance."""
    context = getattr(principal, "demo_context", None)
    return ExecutionOwner(
        principal.tenant_id, product_id, principal.user_id,
        context.instance_id if context is not None else None,
        context.generation if context is not None else None,
    )


def owner_of(context: ProductContext, user_id: str) -> ExecutionOwner:
    return ExecutionOwner(context.tenant_id, context.product_id, user_id)
