"""Comparison of the live turn with the shadow engine's turn, and its counts (5a plan, 5.2 and 5.3).

Every `TurnResponse` field gets exactly one class per compared turn. The shadow's outcome is first
mapped to the live vocabulary (`ShadowView`), then compared field by field:

- `match`: equal after the documented normalization (signal order, clarification key prefix).
- `lifecycle`: the live turn returned a mutation, and the shadow proposed or asked to confirm
  exactly the same translated action; it only stopped at a different point of the lifecycle.
- `platform_wording`: only wording or platform numbers differ (speech, trace reason, signal
  confidence, document titles), with an identical status and action.
- `behaviour`: status, action type, action payload, signals or summary differ.
- `coverage`: the shadow cannot express the live result by design in 5a (a missing translation, a
  team size parsed from free text, or an intent trace label the definition does not declare, such
  as a goal the live engine derived from its own action names).

Turn-level classes (`compared`, `not_compared`, `gated`, `shadow_error`, `over_budget`,
`memory_reset`) are counted under the field name `turn`.

Only counts leave this module: never message text, speech, record values or identifiers of
sessions and visitors. Counts are kept in memory and written in one batched upsert, never on the
request path: at most once a minute from a background task after the response is sent, and once
at shutdown. A failed write is logged and its counts discarded, because losing telemetry is
acceptable and slowing a request is not.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.db import get_connection
from app.definitions.contract import ProductDefinition
from app.engine.conversation_engine import ACTION_STAGES, EngineTurn, TurnStage

logger = logging.getLogger("pixel.shadow")

MATCH = "match"
LIFECYCLE = "lifecycle"
PLATFORM_WORDING = "platform_wording"
BEHAVIOUR = "behaviour"
SECURITY = "security"
COVERAGE = "coverage"
MEMORY_RESET = "memory_reset"
NOT_COMPARED = "not_compared"
GATED = "gated"
SHADOW_ERROR = "shadow_error"
OVER_BUDGET = "over_budget"
COMPARED = "compared"
# Turns the scheduler could not compare (5b plan, section 10).
SHED = "shed"
CIRCUIT_OPEN = "circuit_open"
WORKER_UNHEALTHY = "worker_unhealthy"
CLASSES = frozenset({
    MATCH, LIFECYCLE, PLATFORM_WORDING, BEHAVIOUR, SECURITY, COVERAGE, MEMORY_RESET,
    NOT_COMPARED, GATED, SHADOW_ERROR, OVER_BUDGET, COMPARED, SHED, CIRCUIT_OPEN, WORKER_UNHEALTHY,
})
# Why a turn was not compared or a session was reset, counted under the field `turn_reason`.
TURN_REASON = "turn_reason"
REASONS = frozenset({
    # shed
    "queue_full", "snapshot_too_large", "too_many_in_flight", "worker_unhealthy", "shutdown",
    # not compared
    "no_context_effect", "lost", "stale", "late",
    # memory reset: why the shadow's context for the session is incomplete
    "restart", "epoch", "evicted", "gap_lost", "circuit_open", "error",
})

# Every field of the live response, in schema order (parent plan, exit criterion 9).
FIELDS = (
    "session_id", "turn_id", "status", "speech", "proposed_action", "validated_action", "execution",
    "intent_trace", "signals", "retrieved_context", "session_summary",
)
TURN = "turn"

# Intent trace fields that carry a decision. The rest (current intent, reason, confidence) are
# platform wording and platform numbers.
TRACE_DECISIONS = ("role", "current_tool", "goal", "pain_point", "relevant_feature", "status")
TRACE_WORDING = ("current_intent", "reason", "confidence")
FEATURE_SIGNAL = "feature_interest"
SUMMARY_FIELDS = ("interests", "pain_points", "last_person", "last_feature", "clarification_pending")
# The platform names a pending question by its response key; the live engine drops this prefix.
CLARIFICATION_PREFIX = "clarify_"

FLUSH_INTERVAL_SECONDS = 60
RETENTION_DAYS = 30
PRUNE_BATCH = 500
PRUNE_BATCHES = 20

_UPSERT = """
    insert into shadow_parity_daily(day, tenant_id, product_id, definition_id, definition_version, field, class, count)
    values (?, ?, ?, ?, ?, ?, ?, ?)
    on conflict(day, tenant_id, product_id, definition_id, definition_version, field, class)
    do update set count = count + excluded.count
"""


@dataclass(frozen=True)
class ShadowView:
    """The shadow's turn in the live response's vocabulary (5a plan, 5.2 equivalence table)."""

    status: str
    action: Mapping[str, Any] | None
    action_is_mutation: bool
    # The shadow stopped before execution: it proposed, or asked for confirmation.
    lifecycle_open: bool
    translation_missing: bool
    speech: str
    trace: Mapping[str, Any]
    signals: frozenset[tuple[str, str]]
    confidences: Mapping[tuple[str, str], float]
    sources: tuple[str, ...]
    documents: tuple[tuple[str, str, str], ...]
    summary: Mapping[str, Any]
    # The labels the definition declares for each intent trace field, lowercased.
    declared: Mapping[str, frozenset[str]]


def declared_labels(definition: ProductDefinition) -> dict[str, frozenset[str]]:
    signals = definition.prospect_signals

    def lowered(labels) -> frozenset[str]:
        return frozenset(label.lower() for label in labels)

    return {
        "role": lowered(signals.roles), "current_tool": lowered(signals.current_tools),
        "goal": lowered(signals.goals), "pain_point": lowered(signals.pain_points),
        "relevant_feature": lowered(definition.knowledge_topics),
    }


def shadow_view(turn: EngineTurn, definition: ProductDefinition) -> ShadowView:
    action = None
    if turn.stage in ACTION_STAGES and turn.legacy_action is not None:
        action = {"type": turn.legacy_action.type, "payload": dict(turn.legacy_action.payload)}
    profile = turn.profile
    return ShadowView(
        status="denied" if turn.stage == TurnStage.REFUSED else "completed",
        action=action,
        action_is_mutation=bool(turn.validated and turn.validated.is_mutation),
        lifecycle_open=turn.stage in (TurnStage.PROPOSED, TurnStage.AWAITING_CONFIRMATION),
        translation_missing=turn.translation_missing is not None,
        speech=turn.reply.speech,
        trace={
            "role": profile.role, "current_tool": profile.current_tool, "goal": profile.goal,
            "pain_point": profile.pain_point, "relevant_feature": turn.feature,
            "status": "denied" if turn.stage == TurnStage.REFUSED else "active",
        },
        signals=frozenset((signal.type, signal.value) for signal in turn.signals),
        confidences={(signal.type, signal.value): signal.confidence for signal in turn.signals},
        sources=tuple(passage.source for passage in turn.passages),
        documents=tuple((p.title, p.source, p.snippet) for p in turn.passages),
        summary={
            "interests": list(turn.summary.interests),
            "pain_points": list(turn.summary.pain_points),
            "last_person": turn.summary.last_person,
            "last_feature": turn.summary.last_feature,
            "clarification_pending": turn.summary.clarification_pending,
        },
        declared=declared_labels(definition),
    )


def compare(live: Mapping[str, Any], shadow: ShadowView, *, session_id: str, turn_id: int) -> dict[str, str]:
    """One class per `TurnResponse` field. `live` is the serialized live response."""
    classes: dict[str, str] = {
        "session_id": MATCH if live.get("session_id") == session_id else BEHAVIOUR,
        "turn_id": MATCH if live.get("turn_id") == turn_id else BEHAVIOUR,
        # 5a live responses have no execution field; one appearing is a change to investigate.
        "execution": MATCH if "execution" not in live else BEHAVIOUR,
    }
    both_denied = live.get("status") == "denied" and shadow.status == "denied"
    classes["status"] = MATCH if live.get("status") == shadow.status else BEHAVIOUR
    for name in ("proposed_action", "validated_action"):
        classes[name] = MATCH if both_denied else _action_class(live.get(name), shadow)
    decided = classes["status"] == MATCH and classes["validated_action"] in (MATCH, LIFECYCLE)
    if live.get("speech") == shadow.speech:
        classes["speech"] = MATCH
    else:
        classes["speech"] = PLATFORM_WORDING if decided else BEHAVIOUR
    classes["intent_trace"] = _trace_class(live.get("intent_trace") or {}, shadow)
    classes["signals"] = _signals_class(live.get("signals") or [], shadow)
    classes["retrieved_context"] = _context_class(live.get("retrieved_context") or [], shadow)
    classes["session_summary"] = _summary_class(live.get("session_summary") or {}, shadow)
    assert set(classes) == set(FIELDS)
    return classes


def _action_class(live_action: Mapping[str, Any] | None, shadow: ShadowView) -> str:
    if shadow.translation_missing:
        return COVERAGE
    if not _same(_action(live_action), _action(shadow.action)):
        return BEHAVIOUR
    if shadow.action is not None and shadow.action_is_mutation and shadow.lifecycle_open:
        return LIFECYCLE
    return MATCH


def _action(action: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if action is None:
        return None
    return {"type": action.get("type"), "payload": dict(action.get("payload") or {})}


def _trace_class(live: Mapping[str, Any], shadow: ShadowView) -> str:
    inexpressible = live.get("team_size") is not None
    for name in TRACE_DECISIONS:
        value = _text(live.get(name))
        if value == _text(shadow.trace.get(name)):
            continue
        if value is not None and name in shadow.declared and value not in shadow.declared[name]:
            # The definition has no such label, so no generic engine could produce it.
            inexpressible = True
            continue
        return BEHAVIOUR
    if inexpressible:
        return COVERAGE
    if any(live.get(name) not in (None, 0.0) for name in TRACE_WORDING):
        return PLATFORM_WORDING
    return MATCH


def _signals_class(live: list[Mapping[str, Any]], shadow: ShadowView) -> str:
    pairs = {(item.get("type"), item.get("value")) for item in live}
    undeclared = {pair for pair in pairs if _undeclared_feature(pair[0], pair[1], shadow)}
    if pairs - undeclared != set(shadow.signals):
        return BEHAVIOUR
    if undeclared:
        return COVERAGE
    for item in live:
        if shadow.confidences.get((item.get("type"), item.get("value"))) != item.get("confidence"):
            return PLATFORM_WORDING
    return MATCH


def _context_class(live: list[Mapping[str, Any]], shadow: ShadowView) -> str:
    if tuple(item.get("source") for item in live) != shadow.sources:
        return BEHAVIOUR
    documents = tuple((item.get("title"), item.get("source"), item.get("snippet")) for item in live)
    return MATCH if documents == shadow.documents else PLATFORM_WORDING


def _summary_class(live: Mapping[str, Any], shadow: ShadowView) -> str:
    inexpressible = False
    for name in SUMMARY_FIELDS:
        left, right = live.get(name), shadow.summary.get(name)
        if name == "clarification_pending":
            left, right = _question(left), _question(right)
        if name == "interests" and isinstance(left, list):
            kept = [value for value in left if not _undeclared_feature(FEATURE_SIGNAL, value, shadow)]
            inexpressible |= kept != left
            left = kept
        if name == "last_feature" and _undeclared_feature(FEATURE_SIGNAL, left, shadow):
            inexpressible, left = True, right
        if _same(left if left != [] else None, right if right != [] else None):
            continue
        return BEHAVIOUR
    return COVERAGE if inexpressible else MATCH


def _undeclared_feature(kind: Any, value: Any, shadow: ShadowView) -> bool:
    """A feature the definition does not declare, which no generic engine could report."""
    return (kind == FEATURE_SIGNAL and isinstance(value, str)
            and value.lower() not in shadow.declared.get("relevant_feature", frozenset()))


def _question(key: Any) -> Any:
    if isinstance(key, str) and key.startswith(CLARIFICATION_PREFIX):
        return key[len(CLARIFICATION_PREFIX):]
    return key


def _text(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


def _same(left: Any, right: Any) -> bool:
    return json.dumps(left, sort_keys=True, default=str) == json.dumps(right, sort_keys=True, default=str)


# --- counts ---

@dataclass(frozen=True)
class CountKey:
    day: str
    tenant_id: str
    product_id: str
    definition_id: str
    definition_version: int
    field: str
    cls: str


class ParityCounters:
    """In-memory counts, flushed in one batched upsert off the request path."""

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 today: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._counts: Counter[CountKey] = Counter()
        self._lock = threading.Lock()
        self._clock = clock
        self._today = today
        self._last_scheduled: float | None = None
        self._flushing = False

    def add(self, tenant_id: str, product_id: str, definition_id: str, definition_version: int,
            classes: Mapping[str, str]) -> None:
        day = self._today().strftime("%Y-%m-%d")
        with self._lock:
            for field, cls in classes.items():
                if cls not in (REASONS if field == TURN_REASON else CLASSES):
                    raise ValueError(f"unknown class {cls}")
                self._counts[CountKey(day, tenant_id, product_id, definition_id, definition_version, field, cls)] += 1

    def pending(self) -> dict[CountKey, int]:
        with self._lock:
            return dict(self._counts)

    def take(self) -> dict[CountKey, int]:
        """Swap in an empty map, so no increment is lost or counted twice across a flush."""
        with self._lock:
            taken, self._counts = self._counts, Counter()
        return dict(taken)

    def schedule(self, add_task: Callable[..., Any]) -> bool:
        """Queue a flush after the response, at most once a minute, never two at a time."""
        now = self._clock()
        with self._lock:
            due = self._last_scheduled is None or now - self._last_scheduled >= FLUSH_INTERVAL_SECONDS
            if not self._counts or self._flushing or not due:
                return False
            self._flushing = True
            self._last_scheduled = now
        add_task(self._background_flush)
        return True

    def _background_flush(self) -> None:
        try:
            self.flush()
        finally:
            with self._lock:
                self._flushing = False

    def flush(self) -> int:
        """Write everything counted so far. Returns the number of rows touched; never raises."""
        taken = self.take()
        if not taken:
            return 0
        rows = [
            (key.day, key.tenant_id, key.product_id, key.definition_id, key.definition_version,
             key.field, key.cls, count)
            for key, count in taken.items()
        ]
        try:
            with get_connection() as connection:
                connection.execute("begin immediate")
                connection.executemany(_UPSERT, rows)
                _prune(connection, self._today())
        except Exception as error:  # noqa: BLE001 - telemetry must never fail a caller
            logger.warning("shadow_flush_failed", extra={"error": type(error).__name__, "rows": len(rows)})
            return 0
        return len(rows)


def _prune(connection, today: datetime) -> None:
    cutoff = (today - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    for _ in range(PRUNE_BATCHES):
        removed = connection.execute(
            "delete from shadow_parity_daily where rowid in "
            "(select rowid from shadow_parity_daily where day < ? limit ?)",
            (cutoff, PRUNE_BATCH),
        ).rowcount
        if removed < PRUNE_BATCH:
            return


def report(days: int = 7, today: datetime | None = None) -> list[dict[str, Any]]:
    since = ((today or datetime.now(UTC)) - timedelta(days=max(days, 1) - 1)).strftime("%Y-%m-%d")
    with get_connection() as connection:
        rows = connection.execute(
            """
            select tenant_id, product_id, definition_id, definition_version, field, class, sum(count) as count
            from shadow_parity_daily where day >= ?
            group by tenant_id, product_id, definition_id, definition_version, field, class
            order by tenant_id, product_id, definition_id, definition_version, field, class
            """,
            (since,),
        ).fetchall()
    return [dict(row) for row in rows]
