"""The execution ledger (3.2 plan, section 5).

The platform never writes records itself. When a validated action is sent to a client, it is
recorded here as `dispatched` with a one-time execution key. A keyed write is accepted only
inside one database transaction that, in this order:

1. re-checks the caller's access and the session's definition **now**;
2. claims the key (see the replay contract, section 5.3);
3. changes the record;
4. commits the outcome on the same key row.

Cancellation and execution therefore race on a commit: whichever commits first wins. An
`executed` or `failed` outcome is final, and expiry only stops a key that was never used.

**The ledger stores no customer data.** A row holds identifiers and an outcome: who and what the
key was issued for, a digest of the request it authorizes, the ID of the record that changed and
a result code. A replay reloads that record under the caller's access at that moment instead of
returning a stored copy, which would age, duplicate the record and have to be deleted twice. One
key is therefore one historical attempt, not one stored answer.
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


class ExecutionRefused(Exception):
    """The keyed write must not happen; `reason` is a stable code.

    `conflict` is True when the key itself cannot be used (unknown, mismatched, cancelled,
    expired, already settled) and False when the caller's standing changed.
    """

    def __init__(self, reason: str, conflict: bool = True) -> None:
        super().__init__(reason)
        self.reason = reason
        self.conflict = conflict


class Outcome(StrEnum):
    EXECUTED = ActionState.EXECUTED
    FAILED = ActionState.FAILED


@dataclass(frozen=True)
class ExecutionOwner:
    tenant_id: str
    product_id: str
    user_id: str
    instance_id: str | None = None
    instance_generation: int | None = None


@dataclass(frozen=True)
class DispatchedAction:
    """What the client is told, in this response only. Field values are never stored."""

    execution_key: str
    action_key: str
    capability: str
    entity: str | None
    target_id: str | None
    fields: dict[str, Any]


@dataclass(frozen=True)
class ExecutedAction:
    """A change this session completed, as the ledger remembers it: identifiers, not content."""

    action_key: str
    capability: str
    entity: str | None
    record_id: str | None
    result_code: str | None


@dataclass(frozen=True)
class Replay:
    """A key that already has a committed outcome; the same outcome is reported again."""

    state: ActionState
    record_id: str | None
    result_code: str | None
    reason: str | None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _fields_of(action: GenericAction) -> dict[str, Any]:
    return {name: value for name, value in sorted((action.fields or {}).items())}


def canonical(request: Any) -> str:
    """One stable text for a write request, so dispatch and the endpoint compare the same thing.

    Key order and formatting never matter; values do.
    """
    return json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)


def digest(request: Any) -> str:
    """What the ledger keeps of a request: enough to recognise it, nothing to disclose."""
    return hashlib.sha256(canonical(request).encode("utf-8")).hexdigest()


class ExecutionLedger:
    def dispatch(
        self, validated: ValidatedAction, owner: ExecutionOwner, *, session_id: str, turn_id: int,
        write_request: Any = None, connection=None,
    ) -> DispatchedAction:
        """Record a validated action as dispatched and return its one-time key.

        `write_request` is the request this key authorizes, in the form the endpoint will receive
        it (a product translator produces it). Only its digest is stored, and a write whose
        request has a different digest is refused.
        """
        action = validated.action
        key = token_urlsafe(24)
        row = (
            key, owner.tenant_id, owner.product_id, session_id, turn_id, owner.user_id,
            owner.instance_id, owner.instance_generation,
            action.action_key, str(action.capability), action.target.entity if action.target else None,
            action.target.id if action.target else None, digest(write_request),
            ActionState.DISPATCHED.value, _now(), time.time() + KEY_LIFETIME_SECONDS,
        )
        statement = """
            insert into action_executions(
              execution_key, tenant_id, product_id, session_id, turn_id, user_id,
              instance_id, instance_generation, action_key,
              capability, entity, target_id, request_digest, state, created_at, expires_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        )

    def claim(
        self, connection, execution_key: str, owner: ExecutionOwner, *, request: Any,
        session_id: str | None = None,
    ) -> Replay | None:
        """Take the key for this write, inside the caller's transaction.

        Returns None when the write may proceed, or a `Replay` when the key already has a
        committed outcome. Raises `ExecutionRefused` in every other case.
        """
        row = connection.execute(
            "select * from action_executions where execution_key = ?", (execution_key,)
        ).fetchone()
        if row is None:
            raise ExecutionRefused("unknown_execution_key")
        if (
            row["tenant_id"], row["product_id"], row["user_id"],
            row["instance_id"], row["instance_generation"],
        ) != (
            owner.tenant_id, owner.product_id, owner.user_id,
            owner.instance_id, owner.instance_generation,
        ):
            # Never say the key belongs to someone else.
            raise ExecutionRefused("unknown_execution_key")
        if session_id is not None and row["session_id"] != session_id:
            # The key was issued inside one conversation and is not portable to another.
            raise ExecutionRefused("unknown_execution_key")
        if digest(request) != row["request_digest"]:
            raise ExecutionRefused("execution_request_mismatch")
        state = ActionState(row["state"])
        if state in (ActionState.EXECUTED, ActionState.FAILED):
            # One key is one historical attempt. A settled key reports what happened then,
            # however the data has changed since; a fresh attempt needs a fresh key.
            return Replay(state, row["result_record_id"], row["result_code"], row["reason"])
        if state == ActionState.CANCELLED:
            raise ExecutionRefused("execution_cancelled")
        if row["expires_at"] < time.time():
            raise ExecutionRefused("execution_expired")
        return None

    def settle(self, connection, execution_key: str, outcome: Outcome, *,
               record_id: str | None = None, result_code: str | None = None,
               reason: str | None = None) -> None:
        """Commit the outcome on the key row, in the same transaction as the record change."""
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

    def cancel_turn(self, session_id: str, turn_id: int | None = None, connection=None) -> int:
        """Cancel keys still waiting. A committed outcome is never relabelled.

        This takes no caller, so nothing reachable from a request may call it without first
        checking that the session belongs to that caller.
        """
        statement = (
            "update action_executions set state = ?, settled_at = ? "
            "where session_id = ? and state = ?" + ("" if turn_id is None else " and turn_id = ?")
        )
        values: tuple = (ActionState.CANCELLED.value, _now(), session_id, ActionState.DISPATCHED.value)
        if turn_id is not None:
            values = (*values, turn_id)
        if connection is not None:
            return connection.execute(statement, values).rowcount
        with get_connection() as own:
            return own.execute(statement, values).rowcount

    def state_of(self, execution_key: str) -> ActionState | None:
        with get_connection() as connection:
            row = connection.execute(
                "select state from action_executions where execution_key = ?", (execution_key,)
            ).fetchone()
        return ActionState(row["state"]) if row else None

    def last_executed(self, session_id: str) -> ExecutedAction | None:
        """The most recent change this session completed, named rather than reproduced.

        A caller that needs the record's current content reloads it under its own access.
        """
        with get_connection() as connection:
            row = connection.execute(
                """
                select * from action_executions
                where session_id = ? and state = ?
                order by settled_at desc, rowid desc limit 1
                """,
                (session_id, ActionState.EXECUTED.value),
            ).fetchone()
        if row is None:
            return None
        return ExecutedAction(
            row["action_key"], row["capability"], row["entity"], row["result_record_id"],
            row["result_code"],
        )


def owner_of(context: ProductContext, user_id: str) -> ExecutionOwner:
    return ExecutionOwner(context.tenant_id, context.product_id, user_id)
