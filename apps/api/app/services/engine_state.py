"""Durable definition-engine state (5c plan, section 6): the safe, restart-surviving subset of
`ConversationMemory`, plus a `SignalHistory` rebuilt from the existing `signals` table.

Only identifiers are ever written here: a record reference, a person reference, a view name, an
execution ledger key, a follow-up action key, and a boolean "an unanswered question was pending"
marker. Message text, prompts, retrieved passages and any free-form field value (a new record's
title, a clarification answer) are never duplicated into this table — the plan is explicit that a
restart loses that arbitrary text and asks the visitor to repeat themselves, rather than guessing or
reconstructing it. The full pending state for the *current* process lives only in the in-process
`PendingStateCache` below, which is never durable and is invalidated the moment this row's revision
moves out from under it.

Isolation: every read is checked against the caller's own `ExecutionOwner` after the row comes back,
the same defense-in-depth pattern `ExecutionLedger` and `SessionManager.activate_turn` use elsewhere.
A row that does not match, or was written for a different workspace, is treated as absent.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.db import get_connection
from app.engine.actions import RecordRef
from app.engine.execution import ExecutionOwner
from app.engine.memory import ConversationMemory, PersonFollowUp
from app.engine.signals import EngineSignal, SignalHistory

CODEC_VERSION = 1
HISTORY_LIMIT = 50
# State outlives no session, and no idle row outlives 30 days (5c plan, section 6.3).
RETENTION_SECONDS = 30 * 24 * 60 * 60
PRUNE_BATCH = 500
PRUNE_BATCHES = 20


@dataclass(frozen=True)
class EnginePin:
    definition_id: str
    definition_version: int
    definition_checksum: str
    knowledge_version: int


@dataclass(frozen=True)
class LoadedState:
    memory: ConversationMemory
    revision: int
    pending_requires_repeat: bool


# A visitor's name is stored only while it is short and plain; anything else is not kept.
MAX_VISITOR_NAME = 60


def _forgotten(row) -> LoadedState:
    """Nothing is remembered from this row, but its revision is, so the next write can replace it."""
    return LoadedState(memory=ConversationMemory(), revision=row["revision"], pending_requires_repeat=False)


def _bounded_name(name: str | None) -> str | None:
    if name is None or not name.strip() or len(name) > MAX_VISITOR_NAME or not name.isprintable():
        return None
    return name.strip()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class EngineStateStore:
    """Reads and writes the one durable `engine_state` row for a session."""

    def load(self, connection: sqlite3.Connection, owner: ExecutionOwner, session_id: str,
              scope_id: str, pin: EnginePin) -> LoadedState | None:
        row = connection.execute(
            "select * from engine_state where session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return None
        if (row["tenant_id"], row["product_id"], row["user_id"], row["instance_id"],
                row["instance_generation"]) != owner.values():
            return None
        if row["scope_id"] != scope_id:
            # A workspace switch clears remembered references and pending state (plan, section 6.1).
            # The row itself stays and is overwritten by this turn, so its revision comes back with
            # empty memory: forgetting the old workspace must not cost the session its next turn.
            return _forgotten(row)
        if row["codec_version"] != CODEC_VERSION:
            return _forgotten(row)
        if (row["definition_id"], row["definition_version"], row["definition_checksum"],
                row["knowledge_version"]) != (
            pin.definition_id, pin.definition_version, pin.definition_checksum, pin.knowledge_version
        ):
            # An unknown or stale pin fails closed rather than reusing a possibly-incompatible row.
            return _forgotten(row)
        focus = _ref(row["focus_entity"], row["focus_id"])
        last_person = _ref(row["last_person_entity"], row["last_person_id"])
        follow_up = None
        if row["person_follow_up_action"] is not None:
            follow_up = PersonFollowUp(row["person_follow_up_action"], row["person_follow_up_turn"])
        memory = ConversationMemory(
            focus=focus, last_person=last_person, last_view=row["last_view"],
            last_change=row["last_change"], turn=row["last_turn"], person_follow_up=follow_up,
            visitor_name=row["visitor_name"],
        )
        return LoadedState(memory=memory, revision=row["revision"],
                            pending_requires_repeat=bool(row["pending_requires_repeat"]))

    def commit(self, connection: sqlite3.Connection, owner: ExecutionOwner, session_id: str,
               turn_id: int, scope_id: str, pin: EnginePin, memory: ConversationMemory, *,
               expected_revision: int) -> int | None:
        """Persist the safe subset of `memory`. Returns the new revision, or None if superseded.

        Commits only where the loaded revision still matches and `last_turn < turn_id`, so a
        duplicate or out-of-order finalization can never overwrite newer memory (plan, section 6.3).
        """
        new_revision = expected_revision + 1
        pending = memory.pending_clarification is not None or memory.pending_confirmation is not None
        pending_turn = memory.turn if pending else None
        cursor = connection.execute(
            """
            insert into engine_state(
              session_id, tenant_id, product_id, user_id, instance_id, instance_generation,
              scope_id, codec_version, definition_id, definition_version, definition_checksum,
              knowledge_version, last_turn, revision, focus_entity, focus_id,
              last_person_entity, last_person_id, last_view, last_change,
              person_follow_up_action, person_follow_up_turn, pending_requires_repeat,
              pending_turn, updated_at, visitor_name
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(session_id) do update set
              scope_id = excluded.scope_id, codec_version = excluded.codec_version,
              definition_id = excluded.definition_id, definition_version = excluded.definition_version,
              definition_checksum = excluded.definition_checksum,
              knowledge_version = excluded.knowledge_version,
              last_turn = excluded.last_turn, revision = excluded.revision,
              focus_entity = excluded.focus_entity, focus_id = excluded.focus_id,
              last_person_entity = excluded.last_person_entity, last_person_id = excluded.last_person_id,
              last_view = excluded.last_view, last_change = excluded.last_change,
              person_follow_up_action = excluded.person_follow_up_action,
              person_follow_up_turn = excluded.person_follow_up_turn,
              pending_requires_repeat = excluded.pending_requires_repeat,
              pending_turn = excluded.pending_turn, updated_at = excluded.updated_at,
              visitor_name = excluded.visitor_name
            where engine_state.revision = ? and engine_state.last_turn < ?
            """,
            (
                session_id, owner.tenant_id, owner.product_id, owner.user_id, owner.instance_id,
                owner.instance_generation, scope_id, CODEC_VERSION, pin.definition_id,
                pin.definition_version, pin.definition_checksum, pin.knowledge_version, turn_id,
                new_revision, *_ref_columns(memory.focus), *_ref_columns(memory.last_person),
                memory.last_view, memory.last_change,
                memory.person_follow_up.action_key if memory.person_follow_up else None,
                memory.person_follow_up.turn if memory.person_follow_up else None,
                int(pending), pending_turn, _utc_now(), _bounded_name(memory.visitor_name),
                expected_revision, turn_id,
            ),
        )
        if cursor.rowcount == 0:
            return None
        return new_revision

    def prune(self, now: datetime | None = None, *, batch: int = PRUNE_BATCH, batches: int = PRUNE_BATCHES) -> int:
        """Remove state whose session has expired or ended, or that has not changed in 30 days.

        Bounded: at most `batch * batches` rows per call (5c plan, section 6.3). Returns the count.
        """
        moment = now or datetime.now(UTC)
        stale_before = (moment - timedelta(seconds=RETENTION_SECONDS)).isoformat()
        deleted = 0
        with get_connection() as connection:
            for _ in range(batches):
                cursor = connection.execute(
                    """
                    delete from engine_state where session_id in (
                      select e.session_id from engine_state e join sessions s on s.id = e.session_id
                      where s.ended_at is not null
                         or (s.expires_at is not null and s.expires_at < ?)
                         or e.updated_at < ?
                      limit ?
                    )
                    """,
                    (moment.isoformat(), stale_before, batch),
                )
                deleted += cursor.rowcount
                if cursor.rowcount < batch:
                    break
        return deleted

    def invalidate_sessions(self, connection: sqlite3.Connection, session_ids: list[str]) -> None:
        """Drop durable state for these sessions (private reset, generation change)."""
        if not session_ids:
            return
        connection.executemany(
            "delete from engine_state where session_id = ?", [(sid,) for sid in session_ids]
        )


def rebuild_signal_history(connection: sqlite3.Connection, owner: ExecutionOwner, session_id: str,
                            scope_id: str) -> SignalHistory:
    """Rebuild `SignalHistory` for exactly this owner, session and workspace from durable rows.

    Ordered deterministically by turn, then creation time, then row ID, capped at the most recent
    `HISTORY_LIMIT` rows for this workspace — a workspace switch starts a workspace-local history and
    cannot surface a previous workspace's person or feature (plan, section 6.1).
    """
    rows = connection.execute(
        """
        select s.type, s.value, s.confidence from signals s
        join conversation_owners o on o.session_id = s.session_id
        where s.session_id = ? and s.scope_id = ?
          and o.customer_id = ? and o.product_id = ? and o.user_id = ?
          and o.instance_id is ? and o.instance_generation is ?
        order by s.turn_id, s.created_at, s.id
        """,
        (session_id, scope_id, owner.tenant_id, owner.product_id, owner.user_id,
         owner.instance_id, owner.instance_generation),
    ).fetchall()

    history = SignalHistory()
    signals = [EngineSignal(row["type"], row["value"], row["confidence"]) for row in rows[-HISTORY_LIMIT:]]
    return history.add(signals)


def _ref(entity: str | None, record_id: str | None) -> RecordRef | None:
    return RecordRef(entity, record_id) if entity is not None else None


def _ref_columns(ref: RecordRef | None) -> tuple[str | None, str | None]:
    return (ref.entity, ref.id) if ref is not None else (None, None)


class PendingStateCache:
    """In-process only: full `ConversationMemory` and `SignalHistory`, including arbitrary pending
    text, for continuity within this process between two turns of the same session (plan, section
    6.2 and section 4's "Ephemeral pending-state cache").

    Bounded by entry count and idle time. An entry is usable only while it still matches the
    durable row's current revision; a restart, eviction, expiry, scope change or owner mismatch all
    discard it silently rather than guessing at what the visitor meant.
    """

    MAX_ENTRIES = 1_000
    IDLE_SECONDS = 15 * 60

    def __init__(self, *, limit: int = MAX_ENTRIES, idle_seconds: float = IDLE_SECONDS,
                 clock=time.monotonic) -> None:
        self._limit = limit
        self._idle_seconds = idle_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: "OrderedDict[tuple, tuple[int, ConversationMemory, SignalHistory, float]]" = OrderedDict()

    def _key(self, owner: ExecutionOwner, session_id: str) -> tuple:
        return (*owner.values(), session_id)

    def get(self, owner: ExecutionOwner, session_id: str, *, revision: int
            ) -> tuple[ConversationMemory, SignalHistory] | None:
        key = self._key(owner, session_id)
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            cached_revision, memory, history, cached_at = entry
            if now - cached_at > self._idle_seconds:
                del self._entries[key]
                return None
            if cached_revision != revision:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return memory, history

    def put(self, owner: ExecutionOwner, session_id: str, *, revision: int,
            memory: ConversationMemory, history: SignalHistory) -> None:
        key = self._key(owner, session_id)
        with self._lock:
            self._entries[key] = (revision, memory, history, self._clock())
            self._entries.move_to_end(key)
            while len(self._entries) > self._limit:
                self._entries.popitem(last=False)

    def discard(self, owner: ExecutionOwner, session_id: str) -> None:
        with self._lock:
            self._entries.pop(self._key(owner, session_id), None)
