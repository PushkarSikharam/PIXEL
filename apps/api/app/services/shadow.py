"""The shadow engine: the new engine answers each live turn, and nothing it does is used.

5a built the comparison; 5b (plan section 10) moves it off the request path and keeps it in order.

`main.create_turn` enters this module only when `PIXEL_SHADOW_ENGINE=on`. With the switch off,
nothing here runs.

With the switch on, a turn has two request-path steps, both bounded and non-blocking:

1. `ShadowController.begin`, before the live turn: admission (breaker, worker health, at most 8
   turns in flight per session), registration of the turn, and conversion of the records the live
   turn is about to receive into immutable views.
2. `ShadowController.finish`, after the live response is final: the turn is resolved as having no
   effect on the conversation, as lost context, or as one immutable canonical-JSON payload offered
   to a bounded queue (512 KiB per item, 128 items, 16 MiB in total). It never waits.

One worker thread compares queued turns, per session in turn order, round-robin across sessions.
A session whose lower turn is still in flight is parked for at most 2 seconds without occupying the
worker. A watchdog latches the shadow off if a comparison hangs; Python cannot stop a thread, so the
process must restart before shadow work resumes. The live path is unaffected either way.

Shadow memory lives in `ShadowMemoryStore`, in this process only, and is reset only when context was
really lost: a restart, a new shadow epoch, an evicted entry, or a turn that entered the live engine
but was not compared (section 10.3).

Single-process invariant: the in-process store and scheduler assume one API process. The production
image starts one uvicorn process, and a test fails if that changes.
"""
from __future__ import annotations

import hashlib
import heapq
import json
import logging
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from app.auth import AuthUser
from app.db import get_connection
from app.definitions.organizations import OrganizationDirectory
from app.definitions.registry import DefinitionRegistry
from app.engine.definition_cache import DefinitionCache, Gated, approve
from app.engine.lookup import RecordView
from app.engine.memory import ConversationMemory
from app.engine.router import TurnContext
from app.engine.signals import SignalHistory
from app.services.engine_assembly import PreparedTurn, assemble_engine, prepare_turn, selected_record
from app.services.shadow_parity import (
    CIRCUIT_OPEN,
    COMPARED,
    GATED,
    MEMORY_RESET,
    NOT_COMPARED,
    OVER_BUDGET,
    SHADOW_ERROR,
    SHED,
    TURN,
    TURN_REASON,
    WORKER_UNHEALTHY,
    ParityCounters,
    compare,
    shadow_view,
)

logger = logging.getLogger("pixel.shadow")

SWITCH = "PIXEL_SHADOW_ENGINE"
STORE_LIMIT = 5_000
IDLE_SECONDS = 2 * 60 * 60
LOCK_STRIPES = 64
TOMBSTONE_LIMIT = 10_000
BUDGET_SECONDS = 0.025
# Live outcomes the shadow compares with.
COMPARED_STATUSES = frozenset({"completed", "denied"})
UNKNOWN_DEFINITION = ("-", 0)

# Scheduler limits (section 10.1 and 10.2).
MAX_ITEM_BYTES = 512 * 1024
MAX_ITEMS = 128
MAX_BYTES = 16 * 1024 * 1024
MAX_IN_FLIGHT_PER_SESSION = 8
GAP_SECONDS = 2.0
# Breaker and watchdog (section 10.4).
SLOW_SECONDS = 0.250
SLOW_LIMIT = 5
ERROR_LIMIT = 5
ERROR_WINDOW_SECONDS = 60.0
OPEN_SECONDS = 300.0
WATCHDOG_INTERVAL = 0.250
HANG_SECONDS = 2.0
JOIN_SECONDS = 0.250


@dataclass(frozen=True)
class ShadowKey:
    """Built from the authenticated principal after the live turn's own ownership check passed."""

    tenant_id: str
    product_id: str
    session_id: str
    principal_id: str
    instance_id: str | None
    instance_generation: int


@dataclass(frozen=True)
class ShadowEntry:
    memory: ConversationMemory
    history: SignalHistory
    last_turn: int
    touched_at: float
    # The shadow lost part of this session's context; its remaining turns are `memory_reset`.
    reset: bool = False
    reset_reason: str | None = None


class ShadowMemoryStore:
    """Bounded, in-process, least recently used, with tombstones for entries it dropped.

    A fixed pool of striped locks orders updates to one entry. Tombstones are keyed hashes, not
    keys, and hold no customer content; they let a session whose memory was evicted or expired be
    recognized as reset rather than restarted from nothing.
    """

    def __init__(self, limit: int = STORE_LIMIT, idle_seconds: float = IDLE_SECONDS,
                 clock: Callable[[], float] = time.monotonic, tombstones: int = TOMBSTONE_LIMIT) -> None:
        self._limit = limit
        self._idle = idle_seconds
        self._clock = clock
        self._entries: OrderedDict[ShadowKey, ShadowEntry] = OrderedDict()
        self._tombstones: OrderedDict[bytes, None] = OrderedDict()
        self._tombstone_limit = tombstones
        self._salt = secrets.token_bytes(16)
        self._map_lock = threading.Lock()
        # Created once. Nothing is allocated per session, so evicting leaves nothing behind.
        self.locks: tuple[threading.Lock, ...] = tuple(threading.Lock() for _ in range(LOCK_STRIPES))

    def lock_for(self, key: ShadowKey) -> threading.Lock:
        return self.locks[hash(key) % LOCK_STRIPES]

    def get(self, key: ShadowKey) -> ShadowEntry | None:
        now = self._clock()
        with self._map_lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if now - entry.touched_at > self._idle:
                del self._entries[key]
                self._bury(key)
                return None
            self._entries.move_to_end(key)
            return entry

    def put(self, key: ShadowKey, entry: ShadowEntry) -> None:
        with self._map_lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._limit:
                evicted, _ = self._entries.popitem(last=False)
                self._bury(evicted)

    def mark_reset(self, key: ShadowKey, reason: str) -> None:
        """Record that this session's context is incomplete; the first reason is kept."""
        with self._map_lock:
            current = self._entries.get(key)
            if current is not None and current.reset:
                self._entries.move_to_end(key)
                return
            last = current.last_turn if current is not None else 0
            self._entries[key] = ShadowEntry(ConversationMemory(), SignalHistory(), last, self._clock(),
                                             reset=True, reset_reason=reason)
            self._entries.move_to_end(key)
            while len(self._entries) > self._limit:
                evicted, _ = self._entries.popitem(last=False)
                self._bury(evicted)

    def reset_all(self, reason: str) -> None:
        """Every session loses its memory: nothing may resume incomplete pre-incident context."""
        with self._map_lock:
            for key, entry in list(self._entries.items()):
                if not entry.reset:
                    self._entries[key] = ShadowEntry(ConversationMemory(), SignalHistory(), entry.last_turn,
                                                     entry.touched_at, reset=True, reset_reason=reason)

    def tombstoned(self, key: ShadowKey) -> bool:
        with self._map_lock:
            return self._digest(key) in self._tombstones

    def discard(self, key: ShadowKey) -> None:
        with self._map_lock:
            self._entries.pop(key, None)

    def now(self) -> float:
        return self._clock()

    def __len__(self) -> int:
        with self._map_lock:
            return len(self._entries)

    def _bury(self, key: ShadowKey) -> None:
        digest = self._digest(key)
        self._tombstones[digest] = None
        self._tombstones.move_to_end(digest)
        while len(self._tombstones) > self._tombstone_limit:
            self._tombstones.popitem(last=False)

    def _digest(self, key: ShadowKey) -> bytes:
        return hashlib.blake2b(repr(key).encode(), key=self._salt, digest_size=16).digest()


# What `prepare` fixed before the live turn ran (shared with the 5b test path).
PreparedShadow = PreparedTurn


@dataclass(frozen=True)
class ShadowRequest:
    """The parts of a turn request the comparison uses, rebuilt from a queued payload."""

    session_id: str
    turn_id: int
    product_id: str
    message: str
    selected_issue_id: str | None = None
    workspace_scope_id: str | None = None


@dataclass(frozen=True)
class ShadowResult:
    """For tests and the offline harness only; the live path ignores it."""

    turn_class: str
    classes: Mapping[str, str]
    engine_turn: Any = None
    duration: float = 0.0
    reason: str | None = None


class ShadowRunner:
    """The comparison core: fresh gates, the engine turn, the comparison and the counts."""

    def __init__(
        self,
        directory: OrganizationDirectory,
        pin_for: Callable[[str], Any],
        package_for: Callable[[str], Any],
        counters: ParityCounters | None = None,
        store: ShadowMemoryStore | None = None,
        cache: DefinitionCache | None = None,
        clock: Callable[[], float] = time.perf_counter,
        epoch_started: datetime | None = None,
    ) -> None:
        self._directory = directory
        self._pin_for = pin_for
        self._package_for = package_for
        self.counters = counters or ParityCounters()
        self.store = store or ShadowMemoryStore()
        self.cache = cache or DefinitionCache(directory)
        self._clock = clock
        # Sessions that started before this moment began while the shadow could not see them: a
        # process start, or the switch turned back on (section 10.3).
        self.epoch_started = epoch_started or datetime.now(UTC)

    def package_for(self, definition_id: str) -> Any:
        return self._package_for(definition_id)

    # --- before the live turn ---

    def prepare(self, principal: Any, grant: Any, product_id: str, visible_data: Mapping[str, Any],
                scope_id: str | None = None) -> PreparedShadow | None:
        """Convert the records the live turn will receive. Never raises; None means no shadow."""
        try:
            return self.prepare_or_raise(principal, grant, product_id, visible_data, scope_id)
        except Exception as error:  # noqa: BLE001 - the shadow never affects the live turn
            logger.warning("shadow_error", extra={"stage": "prepare", "error": type(error).__name__})
            try:
                self.counters.add(principal.tenant_id, product_id, *UNKNOWN_DEFINITION, {TURN: SHADOW_ERROR})
            except Exception:  # noqa: BLE001
                pass
            return None

    def prepare_or_raise(self, principal: Any, grant: Any, product_id: str, visible_data: Mapping[str, Any],
                         scope_id: str | None = None) -> PreparedShadow | None:
        """As `prepare`, but a failure raises; None still means the product has no shadow.

        The records are narrowed to the workspace the request selected (see `prepare_turn`); the
        comparison refuses to run if the gates later approve a different definition.
        """
        return prepare_turn(self._directory, self._package_for, principal, grant, product_id,
                            visible_data, scope_id)

    # --- after the live turn ---

    def complete(self, prepared: PreparedShadow | None, principal: Any, request: Any, live: Any) -> ShadowResult:
        """Compare one final live response synchronously (offline harness and tests)."""
        if prepared is None:
            return self.count_turn(principal.tenant_id, request.product_id, SHADOW_ERROR)
        return self.compare_turn(prepared, principal, request, live.model_dump(mode="json"),
                                 shadow_key(principal, request, prepared.grant))

    def compare_turn(self, prepared: PreparedShadow, principal: Any, request: Any,
                     live: Mapping[str, Any], key: ShadowKey) -> ShadowResult:
        """Compare one serialized, final live response. Never raises."""
        started = self._clock()
        definition_id, version = UNKNOWN_DEFINITION
        try:
            if live.get("status") not in COMPARED_STATUSES:
                return self.count_turn(principal.tenant_id, request.product_id, NOT_COMPARED, reason="stale")
            try:
                approval, session_started = self._approve(principal, request)
                if approval.key.definition_id != prepared.definition_id:
                    raise Gated("definition_changed")
                definition = self.cache.definition(approval)
            except Gated:
                return self.count_turn(principal.tenant_id, request.product_id, GATED)
            pin = approval.pin
            definition_id, version = pin.definition_id, pin.definition_version
            with self.store.lock_for(key):
                entry = self.store.get(key)
                reason = self._reset_reason(key, entry, request.turn_id, session_started)
                if reason is not None:
                    self.store.mark_reset(key, reason)
                    turn_class = NOT_COMPARED if reason == "late" else MEMORY_RESET
                    return self.count_turn(principal.tenant_id, request.product_id, turn_class,
                                           reason=reason, definition=(definition_id, version))
                engine, _ = assemble_engine(prepared, definition, pin)
                engine_turn = engine.turn(
                    request.message,
                    entry.memory if entry else ConversationMemory(),
                    entry.history if entry else SignalHistory(),
                    TurnContext(turn=request.turn_id, selected=selected_record(
                        prepared.records, getattr(request, "selected_issue_id", None))),
                )
                self.store.put(key, ShadowEntry(engine_turn.memory, engine_turn.history, request.turn_id,
                                                self.store.now()))
            classes = compare(live, shadow_view(engine_turn, definition),
                              session_id=request.session_id, turn_id=request.turn_id)
            duration = self._clock() - started
            turn_class = OVER_BUDGET if duration > BUDGET_SECONDS else COMPARED
            self.counters.add(principal.tenant_id, request.product_id, definition_id, version,
                              {TURN: turn_class, **classes})
            return ShadowResult(turn_class, classes, engine_turn, duration)
        except Exception as error:  # noqa: BLE001 - the shadow never affects the live turn
            logger.warning("shadow_error", extra={"stage": "compare", "error": type(error).__name__})
            try:
                self.store.mark_reset(key, "error")
                return self.count_turn(principal.tenant_id, request.product_id, SHADOW_ERROR,
                                       definition=(definition_id, version))
            except Exception:  # noqa: BLE001
                return ShadowResult(SHADOW_ERROR, {})

    def count_turn(self, tenant_id: str, product_id: str, turn_class: str, *, reason: str | None = None,
                   definition: tuple[str, int] = UNKNOWN_DEFINITION) -> ShadowResult:
        classes = {TURN: turn_class, **({TURN_REASON: reason} if reason else {})}
        self.counters.add(tenant_id, product_id, definition[0], definition[1], classes)
        return ShadowResult(turn_class, {}, None, 0.0, reason)

    def _reset_reason(self, key: ShadowKey, entry: ShadowEntry | None, turn_id: int,
                      session_started: datetime | None) -> str | None:
        """Why this session's context is incomplete, or None (section 10.3)."""
        if entry is not None:
            if entry.reset:
                return entry.reset_reason or "lost"
            if turn_id <= entry.last_turn:
                # A lower turn arriving after a higher one was compared never rewinds memory.
                return "late"
            return None
        if self.store.tombstoned(key):
            return "evicted"
        if session_started is not None and session_started < self.epoch_started:
            return "restart"
        # Every earlier turn of this session in this epoch had no effect on the conversation (the
        # controller marks any other outcome as lost), so fresh memory is complete.
        return None

    def _approve(self, principal: Any, request: Any):
        """Every gate, read fresh, in one read transaction: one consistent moment, no write."""
        pin = self._pin_for(request.session_id)
        with get_connection() as connection:
            connection.execute("begin")
            try:
                directory = OrganizationDirectory(
                    DefinitionRegistry(self._directory.definitions.source, connection=connection),
                    connection=connection,
                )
                approval = approve(principal, request.product_id, pin, directory)
                row = connection.execute(
                    "select started_at from sessions where id = ?", (request.session_id,)
                ).fetchone()
                started = datetime.fromisoformat(row["started_at"]) if row and row["started_at"] else None
                return approval, started
            finally:
                connection.rollback()

# --- the request-path controller and the worker (section 10) ---

IN_FLIGHT = "in_flight"
QUEUED = "queued"


@dataclass
class AdmittedTurn:
    """One eligible turn between `begin` and `finish`."""

    key: ShadowKey
    turn_id: int
    tenant_id: str
    product_id: str
    principal: Any
    prepared: PreparedShadow
    trial: bool
    prepare_seconds: float
    resolved: bool = False


@dataclass(order=True)
class _Item:
    turn_id: int
    sequence: int
    size: int = field(compare=False)
    payload: bytes = field(compare=False)
    key: ShadowKey = field(compare=False)
    trial: bool = field(compare=False)
    enqueued_at: float = field(compare=False)
    timings: Mapping[str, float] = field(compare=False)


class _Breaker:
    """Recoverable breaker state. Callers hold the controller lock."""

    def __init__(self) -> None:
        self.slow = 0
        self.errors: deque[float] = deque()
        self.open_until: float | None = None
        self.forced = False
        self.trial_out = False

    def admit(self, now: float) -> str:
        if self.forced:
            return "open"
        if self.open_until is not None:
            if now < self.open_until:
                return "open"
            if self.trial_out:
                return "open"
            self.trial_out = True
            return "trial"
        return "ok"

    def record(self, now: float, seconds: float, error: bool, trial: bool) -> bool:
        """Record one completed comparison; True when the breaker has just opened."""
        if trial:
            self.trial_out = False
            if error or seconds > SLOW_SECONDS:
                self.open_until = now + OPEN_SECONDS
                return True
            self.open_until, self.slow = None, 0
            self.errors.clear()
            return False
        if error:
            self.errors.append(now)
            while self.errors and now - self.errors[0] > ERROR_WINDOW_SECONDS:
                self.errors.popleft()
            if len(self.errors) >= ERROR_LIMIT and self.open_until is None:
                self.open_until = now + OPEN_SECONDS
                return True
            return False
        if seconds > SLOW_SECONDS:
            self.slow += 1
            if self.slow >= SLOW_LIMIT and self.open_until is None:
                self.open_until = now + OPEN_SECONDS
                return True
            return False
        self.slow = 0
        return False

    def release_trial(self) -> None:
        self.trial_out = False


class ShadowController:
    """Admission, the byte-bounded queue, the session-order scheduler, the worker and the watchdog."""

    def __init__(
        self,
        runner: ShadowRunner,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_item_bytes: int = MAX_ITEM_BYTES,
        max_items: int = MAX_ITEMS,
        max_bytes: int = MAX_BYTES,
        max_in_flight: int = MAX_IN_FLIGHT_PER_SESSION,
        gap_seconds: float = GAP_SECONDS,
        hang_seconds: float = HANG_SECONDS,
        watchdog_interval: float = WATCHDOG_INTERVAL,
        start: bool = True,
    ) -> None:
        self.runner = runner
        self.counters = runner.counters
        self._clock = clock
        self._max_item_bytes = max_item_bytes
        self._max_items = max_items
        self._max_bytes = max_bytes
        self._max_in_flight = max_in_flight
        self._gap = gap_seconds
        self._hang = hang_seconds
        self._watchdog_interval = watchdog_interval
        self._cond = threading.Condition()
        self._registrations: dict[ShadowKey, dict[int, str]] = {}
        self._heaps: dict[ShadowKey, list[_Item]] = {}
        self._ready: deque[ShadowKey] = deque()
        self._ready_set: set[ShadowKey] = set()
        # Sessions waiting for a lower turn, and the deadline of each wait. The deadline is fixed
        # when a session first parks and kept until its lowest item runs, so re-parking never
        # extends the 2-second limit.
        self._parked: dict[ShadowKey, float] = {}
        self._deadlines: dict[ShadowKey, float] = {}
        self._sequence = 0
        self._items = 0
        self._bytes = 0
        self._breaker = _Breaker()
        self._unhealthy = False
        self._closed = False
        self._stop = threading.Event()
        self._current: tuple[ShadowKey, float] | None = None
        self._worker: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        if start:
            self.start()

    # --- lifecycle ---

    def start(self) -> None:
        self._worker = threading.Thread(target=self._work, name="pixel-shadow-worker", daemon=True)
        self._watchdog = threading.Thread(target=self._watch, name="pixel-shadow-watchdog", daemon=True)
        self._worker.start()
        self._watchdog.start()

    def new_epoch(self, started: datetime) -> None:
        """The switch came back on: no existing conversation resumes against incomplete memory."""
        self.runner.epoch_started = started
        self.runner.store.reset_all("epoch")

    def close(self) -> None:
        """Stop admission, shed what is queued, stop both threads briefly, flush counts."""
        with self._cond:
            self._closed = True
            self._drain_locked(SHED, "shutdown")
            self._stop.set()
            self._cond.notify_all()
        for thread in (self._worker, self._watchdog):
            if thread is not None and thread is not threading.current_thread():
                thread.join(JOIN_SECONDS)
        self.counters.flush()

    @property
    def healthy(self) -> bool:
        with self._cond:
            return not self._unhealthy

    def queue_state(self) -> tuple[int, int]:
        with self._cond:
            return self._items, self._bytes

    def wait_until_idle(self, timeout: float) -> bool:
        """True once nothing is queued, parked or being compared (tests and diagnostics)."""
        deadline = self._clock() + timeout
        with self._cond:
            while self._items or self._current is not None:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return False
                self._cond.wait(min(remaining, WATCHDOG_INTERVAL))
            return True

    # --- request path ---

    def begin(self, principal: Any, grant: Any, request: Any, visible_data: Mapping[str, Any]) -> AdmittedTurn | None:
        """Admit and prepare one eligible turn. Never raises; None means no shadow for this turn."""
        started = self._clock()
        try:
            key = shadow_key(principal, request, grant)
            with self._cond:
                if self._closed:
                    return None
                if self._unhealthy:
                    self._lose_locked(key, principal, request, WORKER_UNHEALTHY, "worker_unhealthy")
                    return None
                admitted = self._breaker.admit(self._clock())
                if admitted == "open":
                    self._lose_locked(key, principal, request, CIRCUIT_OPEN, "circuit_open")
                    return None
                registrations = self._registrations.setdefault(key, {})
                if len(registrations) >= self._max_in_flight:
                    if admitted == "trial":
                        self._breaker.release_trial()
                    self._lose_locked(key, principal, request, SHED, "too_many_in_flight")
                    return None
                registrations[request.turn_id] = IN_FLIGHT
            try:
                prepared = self.runner.prepare_or_raise(principal, grant, request.product_id, visible_data,
                                                        getattr(request, "workspace_scope_id", None))
            except Exception as error:  # noqa: BLE001
                logger.warning("shadow_error", extra={"stage": "prepare", "error": type(error).__name__})
                with self._cond:
                    self._resolve_locked(key, request.turn_id, lost="error")
                    if admitted == "trial":
                        self._breaker.release_trial()
                self.runner.count_turn(principal.tenant_id, request.product_id, SHADOW_ERROR)
                return None
            if prepared is None:
                # This product has no shadow: nothing was lost.
                with self._cond:
                    self._resolve_locked(key, request.turn_id)
                    if admitted == "trial":
                        self._breaker.release_trial()
                return None
            return AdmittedTurn(key, request.turn_id, principal.tenant_id, request.product_id, principal, prepared,
                          admitted == "trial", self._clock() - started)
        except Exception as error:  # noqa: BLE001 - the shadow never affects the live turn
            logger.warning("shadow_error", extra={"stage": "begin", "error": type(error).__name__})
            return None

    def finish(self, admitted: AdmittedTurn | None, request: Any, response: Any) -> None:
        """Resolve the turn after the live response is final. Never raises and never waits."""
        if admitted is None or admitted.resolved:
            return
        try:
            status = response.status
            entered = getattr(response, "_engine_entered", True)
            if not entered:
                self._settle(admitted, None, NOT_COMPARED, "no_context_effect")
                return
            if status not in COMPARED_STATUSES:
                # The live engine ran and its turn was discarded: its effect on the conversation is
                # unknown to the shadow, so the session's context is lost.
                self._settle(admitted, "lost", NOT_COMPARED, "lost")
                return
            serialize_started = self._clock()
            payload = _serialize(admitted, request, response.model_dump(mode="json"))
            serialize_seconds = self._clock() - serialize_started
            self._enqueue(admitted, payload, serialize_seconds)
        except Exception as error:  # noqa: BLE001
            logger.warning("shadow_error", extra={"stage": "finish", "error": type(error).__name__})
            self._settle(admitted, "error", SHADOW_ERROR, None)

    def abandon(self, admitted: AdmittedTurn | None) -> None:
        """The live turn raised: its effect on the conversation is unknown."""
        if admitted is None or admitted.resolved:
            return
        self._settle(admitted, "lost", NOT_COMPARED, "lost")

    def _settle(self, admitted: AdmittedTurn, lost: str | None, turn_class: str, reason: str | None) -> None:
        admitted.resolved = True
        with self._cond:
            self._resolve_locked(admitted.key, admitted.turn_id, lost=lost)
            if admitted.trial:
                self._breaker.release_trial()
        self.runner.count_turn(admitted.tenant_id, admitted.product_id, turn_class, reason=reason)

    def _enqueue(self, admitted: AdmittedTurn, payload: bytes, serialize_seconds: float) -> None:
        enqueue_started = self._clock()
        size = len(payload)
        with self._cond:
            admitted.resolved = True
            if self._closed or self._unhealthy:
                reason = "shutdown" if self._closed else "worker_unhealthy"
            elif size > self._max_item_bytes:
                reason = "snapshot_too_large"
            elif self._items + 1 > self._max_items or self._bytes + size > self._max_bytes:
                reason = "queue_full"
            else:
                reason = None
            if reason is not None:
                self._resolve_locked(admitted.key, admitted.turn_id, lost=reason)
                if admitted.trial:
                    self._breaker.release_trial()
            else:
                self._sequence += 1
                item = _Item(admitted.turn_id, self._sequence, size, payload, admitted.key, admitted.trial,
                             self._clock(), MappingProxyType({
                                 "prepare_ms": round(admitted.prepare_seconds * 1000, 3),
                                 "serialize_ms": round(serialize_seconds * 1000, 3),
                                 "enqueue_ms": round((self._clock() - enqueue_started) * 1000, 3),
                             }))
                self._items += 1
                self._bytes += size
                self._registrations.setdefault(admitted.key, {})[admitted.turn_id] = QUEUED
                heapq.heappush(self._heaps.setdefault(admitted.key, []), item)
                # A new arrival may be the turn a parked session was waiting for.
                self._parked.pop(admitted.key, None)
                self._make_ready_locked(admitted.key)
                self._cond.notify_all()
        if reason is not None:
            self.runner.count_turn(admitted.tenant_id, admitted.product_id, SHED, reason=reason)

    # --- state transitions (controller lock held) ---

    def _lose_locked(self, key: ShadowKey, principal: Any, request: Any, turn_class: str, reason: str) -> None:
        self.runner.store.mark_reset(key, reason)
        self.runner.count_turn(principal.tenant_id, request.product_id, turn_class, reason=reason)

    def _resolve_locked(self, key: ShadowKey, turn_id: int, *, lost: str | None = None) -> None:
        registrations = self._registrations.get(key)
        if registrations is not None:
            registrations.pop(turn_id, None)
            if not registrations and not self._heaps.get(key):
                self._registrations.pop(key, None)
        if lost is not None:
            self.runner.store.mark_reset(key, lost)
        if key in self._parked:
            del self._parked[key]
            self._make_ready_locked(key)
        self._cond.notify_all()

    def _make_ready_locked(self, key: ShadowKey) -> None:
        if key not in self._ready_set and key not in self._parked and self._heaps.get(key):
            self._ready.append(key)
            self._ready_set.add(key)

    def _drain_locked(self, turn_class: str, reason: str) -> None:
        for key, heap in list(self._heaps.items()):
            while heap:
                item = heapq.heappop(heap)
                self._items -= 1
                self._bytes -= item.size
                self._registrations.get(key, {}).pop(item.turn_id, None)
                self.runner.store.mark_reset(key, reason)
                self.runner.count_turn(key.tenant_id, key.product_id, turn_class, reason=reason)
            self._heaps.pop(key, None)
        self._ready.clear()
        self._ready_set.clear()
        self._parked.clear()
        self._deadlines.clear()

    def _next_locked(self, now: float) -> _Item | None:
        """The next item whose lower turns are all compared or proven to have no effect."""
        for key, deadline in list(self._parked.items()):
            if now >= deadline:
                # The lower turn never arrived: the waiting item cannot be compared honestly. The
                # missing turns stop blocking; if one arrives later, the reset session absorbs it.
                del self._parked[key]
                self._deadlines.pop(key, None)
                self.runner.store.mark_reset(key, "gap_lost")
                heap = self._heaps.get(key)
                lowest = heap[0].turn_id if heap else None
                registrations = self._registrations.get(key, {})
                for turn in [turn for turn, state in registrations.items()
                             if state == IN_FLIGHT and (lowest is None or turn < lowest)]:
                    del registrations[turn]
                self._make_ready_locked(key)
        for _ in range(len(self._ready)):
            key = self._ready.popleft()
            self._ready_set.discard(key)
            heap = self._heaps.get(key)
            if not heap:
                continue
            lowest = heap[0].turn_id
            waiting = any(turn < lowest and state == IN_FLIGHT
                          for turn, state in self._registrations.get(key, {}).items())
            if waiting:
                self._parked[key] = self._deadlines.setdefault(key, now + self._gap)
                continue
            item = heapq.heappop(heap)
            self._deadlines.pop(key, None)
            if heap:
                self._make_ready_locked(key)
            else:
                self._heaps.pop(key, None)
            return item
        return None

    # --- worker and watchdog ---

    def _work(self) -> None:
        while True:
            with self._cond:
                item = None
                while not self._stop.is_set():
                    item = self._next_locked(self._clock())
                    if item is not None:
                        break
                    timeout = WATCHDOG_INTERVAL
                    if self._parked:
                        timeout = max(0.0, min(min(self._parked.values()) - self._clock(), timeout))
                    self._cond.wait(timeout)
                if self._stop.is_set() or item is None:
                    return
                self._current = (item.key, self._clock())
            error = False
            started = self._clock()
            try:
                result = self._compare(item)
                error = result.turn_class == SHADOW_ERROR
            except Exception as failure:  # noqa: BLE001
                error = True
                logger.warning("shadow_error", extra={"stage": "worker", "error": type(failure).__name__})
            seconds = self._clock() - started
            with self._cond:
                self._current = None
                self._items -= 1
                self._bytes -= item.size
                self._resolve_locked(item.key, item.turn_id)
                opened = self._breaker.record(self._clock(), seconds, error, item.trial)
                if opened:
                    self.runner.store.reset_all("circuit_open")
                logger.info("shadow_turn", extra={
                    **item.timings,
                    "queue_wait_ms": round((started - item.enqueued_at) * 1000, 3),
                    "compare_ms": round(seconds * 1000, 3),
                    "error": error,
                })
                self._cond.notify_all()

    def _compare(self, item: _Item) -> ShadowResult:
        principal, request, live, prepared = _deserialize(item.payload, self.runner)
        return self.runner.compare_turn(prepared, principal, request, live, item.key)

    def _watch(self) -> None:
        while not self._stop.wait(self._watchdog_interval):
            with self._cond:
                current = self._current
                if current is None or self._unhealthy:
                    continue
                if self._clock() - current[1] > self._hang:
                    self._unhealthy = True
                    self._breaker.forced = True
                    self._drain_locked(SHED, "worker_unhealthy")
                    self.runner.store.reset_all("worker_unhealthy")
                    logger.error("shadow_worker_unhealthy", extra={
                        "hung_seconds": round(self._clock() - current[1], 3),
                        "action": "restart the API process to resume shadow work",
                    })
                    self._cond.notify_all()


# --- payload ---

def shadow_key(principal: Any, request: Any, grant: Any) -> ShadowKey:
    context = getattr(grant, "demo_context", None)
    return ShadowKey(
        tenant_id=principal.tenant_id,
        product_id=request.product_id,
        session_id=request.session_id,
        principal_id=principal.user_id,
        instance_id=context.instance_id if context is not None else None,
        instance_generation=context.generation if context is not None else 0,
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(item) for item in value)
    return value


def _serialize(admitted: AdmittedTurn, request: Any, live: Mapping[str, Any]) -> bytes:
    """One immutable canonical-JSON payload: nothing mutable crosses to the worker."""
    prepared = admitted.prepared
    document = {
        "principal": asdict(admitted.principal) if isinstance(admitted.principal, AuthUser) else {
            name: getattr(admitted.principal, name, None)
            for name in ("kind", "user_id", "tenant_id", "role", "team_id", "product_id")
        },
        "request": {
            "session_id": request.session_id, "turn_id": request.turn_id, "product_id": request.product_id,
            "message": request.message, "selected_issue_id": getattr(request, "selected_issue_id", None),
            "workspace_scope_id": getattr(request, "workspace_scope_id", None),
        },
        "live": live,
        "prepared": {
            "definition_id": prepared.definition_id,
            "scope_label": prepared.scope_label,
            "records": {
                entity: [{"id": view.id, "title": view.title, "fields": _plain(view.fields)} for view in views]
                for entity, views in prepared.records.items()
            },
        },
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _deserialize(payload: bytes, runner: ShadowRunner) -> tuple[Any, ShadowRequest, dict, PreparedShadow]:
    document = json.loads(payload)
    principal_fields = document["principal"]
    principal = AuthUser(**{name: principal_fields.get(name) for name in AuthUser.__dataclass_fields__
                            if name in principal_fields})
    request = ShadowRequest(**document["request"])
    prepared_fields = document["prepared"]
    records = MappingProxyType({
        entity: tuple(RecordView(entity, row["id"], row["title"], row["fields"]) for row in rows)
        for entity, rows in prepared_fields["records"].items()
    })
    prepared = PreparedShadow(prepared_fields["definition_id"], records, prepared_fields["scope_label"],
                              runner.package_for(prepared_fields["definition_id"]), None)
    return principal, request, document["live"], prepared
