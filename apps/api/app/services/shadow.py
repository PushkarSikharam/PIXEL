"""The shadow engine: the new engine answers each live turn, and nothing it does is used (5a plan).

`main.create_turn` enters this module only when `PIXEL_SHADOW_ENGINE=on`. With the switch off,
nothing here runs: no snapshot, no copy, no lock.

With the switch on, a turn has two steps:

1. `prepare`, before the live turn: the product package converts the records the live turn is
   about to receive into immutable views. That conversion is the only copy, so both engines see the
   same records and neither can change what the other saw.
2. `complete`, after the live response is final: fresh gates, the engine turn, the comparison, and
   in-memory counts. No database write happens here, and nothing is returned to the caller. Any
   failure is counted as `shadow_error` and swallowed; it can never change the live response.

Shadow memory lives in `ShadowMemoryStore`, in this process only. Nothing the live engine reads or
writes is reachable from it, and it has no method that takes a database connection.

Single-process invariant: the in-process store (like the rate limiter) assumes one API process.
The production image starts one uvicorn process, and a test fails if that changes. Running more
than one process first requires moving shadow memory to a shared store (5c).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from app.db import get_connection
from app.definitions.organizations import OrganizationDirectory
from app.definitions.registry import DefinitionRegistry
from app.engine.actions import RecordRef
from app.engine.conversation import CapabilityPolicy
from app.engine.conversation_engine import ConversationEngine
from app.engine.definition_cache import DefinitionCache, Gated, approve
from app.engine.knowledge import KnowledgeContext
from app.engine.lookup import RecordView
from app.engine.memory import ConversationMemory
from app.engine.router import TurnContext
from app.engine.signals import SignalHistory
from app.engine.snapshot import LoadedRecordSource, TurnSnapshot
from app.services.shadow_parity import (
    COMPARED,
    GATED,
    MEMORY_RESET,
    NOT_COMPARED,
    OVER_BUDGET,
    SHADOW_ERROR,
    TURN,
    ParityCounters,
    compare,
    shadow_view,
)

logger = logging.getLogger("pixel.shadow")

SWITCH = "PIXEL_SHADOW_ENGINE"
STORE_LIMIT = 5_000
IDLE_SECONDS = 2 * 60 * 60
LOCK_STRIPES = 64
BUDGET_SECONDS = 0.025
# Live outcomes the shadow compares with. Stale and cancelled turns never advance shadow memory.
COMPARED_STATUSES = frozenset({"completed", "denied"})
UNKNOWN_DEFINITION = ("-", 0)


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
    # The shadow lost this session's earlier turns (a restart, an eviction or an expiry); its
    # later turns are counted as `memory_reset` instead of producing false differences.
    reset: bool = False


class ShadowMemoryStore:
    """Bounded, in-process, least recently used. A fixed pool of striped locks orders updates."""

    def __init__(self, limit: int = STORE_LIMIT, idle_seconds: float = IDLE_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._idle = idle_seconds
        self._clock = clock
        self._entries: OrderedDict[ShadowKey, ShadowEntry] = OrderedDict()
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
                return None
            self._entries.move_to_end(key)
            return entry

    def put(self, key: ShadowKey, entry: ShadowEntry) -> None:
        with self._map_lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._limit:
                self._entries.popitem(last=False)

    def discard(self, key: ShadowKey) -> None:
        with self._map_lock:
            self._entries.pop(key, None)

    def now(self) -> float:
        return self._clock()

    def __len__(self) -> int:
        with self._map_lock:
            return len(self._entries)


@dataclass(frozen=True)
class PreparedShadow:
    """What `prepare` fixed before the live turn ran."""

    definition_id: str
    records: Mapping[str, tuple[RecordView, ...]]
    scope_label: str
    package: Any
    grant: Any


@dataclass(frozen=True)
class ShadowResult:
    """For tests and the offline harness only; the live path ignores it."""

    turn_class: str
    classes: Mapping[str, str]
    engine_turn: Any = None
    duration: float = 0.0


class ShadowRunner:
    def __init__(
        self,
        directory: OrganizationDirectory,
        pin_for: Callable[[str], Any],
        package_for: Callable[[str], Any],
        counters: ParityCounters | None = None,
        store: ShadowMemoryStore | None = None,
        cache: DefinitionCache | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._directory = directory
        self._pin_for = pin_for
        self._package_for = package_for
        self.counters = counters or ParityCounters()
        self.store = store or ShadowMemoryStore()
        self.cache = cache or DefinitionCache(directory)
        self._clock = clock

    # --- before the live turn ---

    def prepare(self, principal: Any, grant: Any, product_id: str, visible_data: Mapping[str, Any],
                scope_id: str | None = None) -> PreparedShadow | None:
        """Convert the records the live turn will receive. Never raises; None means no shadow.

        `scope_id` is the workspace the request selected. The live engine answers inside that one
        workspace, so the shadow's records are narrowed to it as well: a shadow wider than the
        live turn would propose changes the live turn refuses. The product's package is chosen by
        the definition its binding names now; `complete` refuses to compare if the gates later
        approve a different definition.
        """
        try:
            if scope_id is not None:
                if not grant.may_use(scope_id):
                    return None
                grant = replace(grant, scope_ids=frozenset({scope_id}), is_admin=False)
            binding = self._directory.product(principal.tenant_id, product_id)
            if binding is None:
                return None
            package = self._package_for(binding.definition_id)
            if package.lookup_factory is None:
                return None
            source = package.lookup_factory(grant)
            if not isinstance(source, LoadedRecordSource):
                return None
            records = MappingProxyType({
                entity: tuple(views) for entity, views in source.records_from(visible_data).items()
            })
            return PreparedShadow(binding.definition_id, records, source.scope_label, package, grant)
        except Exception as error:  # noqa: BLE001 - the shadow never affects the live turn
            logger.warning("shadow_error", extra={"stage": "prepare", "error": type(error).__name__})
            try:
                self.counters.add(principal.tenant_id, product_id, *UNKNOWN_DEFINITION, {TURN: SHADOW_ERROR})
            except Exception:  # noqa: BLE001
                pass
            return None

    # --- after the live turn ---

    def complete(self, prepared: PreparedShadow | None, principal: Any, request: Any,
                 live: Any) -> ShadowResult:
        """Compare one final live response. Never raises and never changes `live`."""
        started = self._clock()
        definition_id, version = UNKNOWN_DEFINITION
        try:
            if prepared is None:
                return self._count(principal, request, definition_id, version, SHADOW_ERROR, {}, started)
            serialized = live.model_dump(mode="json")
            if serialized.get("status") not in COMPARED_STATUSES:
                return self._count(principal, request, definition_id, version, NOT_COMPARED, {}, started)
            try:
                approval = self._approve(principal, request)
                if approval.key.definition_id != prepared.definition_id:
                    raise Gated("definition_changed")
                definition = self.cache.definition(approval)
            except Gated:
                return self._count(principal, request, definition_id, version, GATED, {}, started)
            pin = approval.pin
            definition_id, version = pin.definition_id, pin.definition_version
            key = _key(principal, request, prepared.grant)
            with self.store.lock_for(key):
                entry = self.store.get(key)
                if entry is not None and entry.reset:
                    self.store.put(key, _touched(entry, request.turn_id, self.store.now()))
                    return self._count(principal, request, definition_id, version, MEMORY_RESET, {}, started)
                if entry is None and request.turn_id > 1:
                    self.store.put(key, ShadowEntry(ConversationMemory(), SignalHistory(), request.turn_id,
                                                    self.store.now(), reset=True))
                    return self._count(principal, request, definition_id, version, MEMORY_RESET, {}, started)
                if entry is not None and request.turn_id <= entry.last_turn:
                    return self._count(principal, request, definition_id, version, NOT_COMPARED, {}, started)
                engine_turn = self._engine(prepared, definition, pin).turn(
                    request.message,
                    entry.memory if entry else ConversationMemory(),
                    entry.history if entry else SignalHistory(),
                    TurnContext(turn=request.turn_id, selected=_selected(prepared.records, request)),
                )
                self.store.put(key, ShadowEntry(engine_turn.memory, engine_turn.history, request.turn_id,
                                                self.store.now()))
            classes = compare(serialized, shadow_view(engine_turn, definition),
                              session_id=request.session_id, turn_id=request.turn_id)
            duration = self._clock() - started
            turn_class = OVER_BUDGET if duration > BUDGET_SECONDS else COMPARED
            self.counters.add(principal.tenant_id, request.product_id, definition_id, version,
                              {TURN: turn_class, **classes})
            logger.info("shadow_turn", extra={
                "turn_class": turn_class, "classes": dict(classes), "duration_ms": round(duration * 1000, 2),
                "definition_version": version,
            })
            return ShadowResult(turn_class, classes, engine_turn, duration)
        except Exception as error:  # noqa: BLE001 - the shadow never affects the live turn
            logger.warning("shadow_error", extra={"stage": "complete", "error": type(error).__name__})
            try:
                return self._count(principal, request, definition_id, version, SHADOW_ERROR, {}, started)
            except Exception:  # noqa: BLE001
                return ShadowResult(SHADOW_ERROR, {})

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
                return approve(principal, request.product_id, pin, directory)
            finally:
                connection.rollback()

    def _count(self, principal: Any, request: Any, definition_id: str, version: int, turn_class: str,
               classes: Mapping[str, str], started: float) -> ShadowResult:
        self.counters.add(principal.tenant_id, request.product_id, definition_id, version, {TURN: turn_class})
        return ShadowResult(turn_class, classes, None, self._clock() - started)

    def _engine(self, prepared: PreparedShadow, definition: Any, pin: Any) -> ConversationEngine:
        people = definition.people
        snapshot = TurnSnapshot(
            records=prepared.records,
            scope_label=prepared.scope_label,
            definition_checksum=pin.definition_checksum,
            taken_at=time.time(),
            people_entity=people.entity if people else None,
            person_fields=_person_fields(people.assigned_by if people else ()),
        )
        package = prepared.package
        translator = package.legacy_translator(snapshot) if package.legacy_translator else None
        can_translate = getattr(translator, "can_translate", None)
        policy = CapabilityPolicy(
            # Offer only what the installed app can express; fail closed without a translator.
            translatable=can_translate if callable(can_translate) else (lambda key: False),
            # Record visibility is enforced by the snapshot; every declared action is otherwise
            # available to a caller who passed the product gates.
            permitted=lambda key: True,
        )
        knowledge = None
        if package.knowledge_factory is not None:
            knowledge = package.knowledge_factory(KnowledgeContext(
                tenant_id=pin.tenant_id, product_id=pin.product_id, definition_id=pin.definition_id,
                definition_version=pin.definition_version, definition_checksum=pin.definition_checksum,
                knowledge_version=pin.knowledge_version, scope_label=prepared.scope_label or "all",
            ))
        return ConversationEngine(
            definition, snapshot, policy, knowledge,
            definition_version=pin.definition_version,
            translate=translator.translate if translator is not None else None,
        )


def _key(principal: Any, request: Any, grant: Any) -> ShadowKey:
    context = getattr(grant, "demo_context", None)
    return ShadowKey(
        tenant_id=principal.tenant_id,
        product_id=request.product_id,
        session_id=request.session_id,
        principal_id=principal.user_id,
        instance_id=context.instance_id if context is not None else None,
        instance_generation=context.generation if context is not None else 0,
    )


def _touched(entry: ShadowEntry, turn: int, now: float) -> ShadowEntry:
    return ShadowEntry(entry.memory, entry.history, max(turn, entry.last_turn), now, entry.reset)


def _selected(records: Mapping[str, tuple[RecordView, ...]], request: Any) -> RecordRef | None:
    """The record the visitor has open, if exactly one visible record has that ID."""
    record_id = getattr(request, "selected_issue_id", None)
    if not record_id:
        return None
    wanted = record_id.lower()
    found = [RecordRef(entity, view.id) for entity, views in records.items() for view in views
             if view.id.lower() == wanted]
    return found[0] if len(found) == 1 else None


def _person_fields(assigned_by: Any) -> dict[str, str]:
    """`entity.field` style declarations, as entity -> field."""
    fields: dict[str, str] = {}
    for reference in assigned_by:
        entity, _, field = str(reference).partition(".")
        if entity and field:
            fields.setdefault(entity, field)
    return fields
