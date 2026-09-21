"""Signals, the prospect profile and the session summary (5a plan, section 4.4).

Everything here is driven by the definition: `knowledge_topics` name the features a visitor can
show interest in, and `prospect_signals` name roles, current tools, goals and pain points. Terms
are matched literally, on word boundaries, against the corrected request (so "not billing, show me
the records" is about records). There is no product vocabulary in this module.

The feature of a turn is the topic of the action the visitor is being shown, when the definition
names one; otherwise the first topic the message mentions. "What about Alice" after an entity list
is about entities although it names no topic.

Accumulation is an explicit value, `SignalHistory`, passed into a turn and returned from it. It is
never hidden state, so the engine stays a pure function of its inputs.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from app.definitions.contract import ProductDefinition
from app.engine.memory import ConversationMemory
from app.engine.normalizer import NormalizedMessage, contains_term

FEATURE_INTEREST = "feature_interest"
PAIN_POINT = "pain_point"
PERSON_INTEREST = "person_interest"

# Fixed platform confidences. They describe how the signal was found, not how sure a model is:
# a literal term match, a pain point stated in the visitor's words, a person the request is about.
FEATURE_CONFIDENCE = 0.7
PAIN_POINT_CONFIDENCE = 0.78
PERSON_CONFIDENCE = 0.84

HISTORY_LIMIT = 50


@dataclass(frozen=True)
class EngineSignal:
    type: str
    value: str
    confidence: float


@dataclass(frozen=True)
class ProspectProfile:
    """What the visitor said about themselves on this turn. Each field is a declared label."""

    role: str | None = None
    current_tool: str | None = None
    goal: str | None = None
    pain_point: str | None = None


@dataclass(frozen=True)
class SignalHistory:
    """The distinct signals seen so far in a session, as (type, value) pairs.

    `seen` keeps first-seen order and holds at most `HISTORY_LIMIT` pairs, dropping the oldest.
    `latest` holds the most recent value of each type, which is what "last person" and "last
    feature" mean. Both are tuples, so a history can be shared and compared but never changed.
    """

    seen: tuple[tuple[str, str], ...] = ()
    latest: tuple[tuple[str, str], ...] = ()

    def add(self, signals: Iterable[EngineSignal]) -> "SignalHistory":
        seen = list(self.seen)
        latest = dict(self.latest)
        for signal in signals:
            pair = (signal.type, signal.value)
            if pair not in seen:
                seen.append(pair)
            latest.pop(signal.type, None)
            latest[signal.type] = signal.value
        return SignalHistory(tuple(seen[-HISTORY_LIMIT:]), tuple(latest.items()))

    def values(self, signal_type: str) -> tuple[str, ...]:
        return tuple(value for kind, value in self.seen if kind == signal_type)

    def latest_value(self, signal_type: str) -> str | None:
        return dict(self.latest).get(signal_type)


@dataclass(frozen=True)
class EngineSummary:
    """The session summary, computed from the next memory and the next history only."""

    interests: tuple[str, ...] = ()
    pain_points: tuple[str, ...] = ()
    last_person: str | None = None
    last_feature: str | None = None
    clarification_pending: str | None = None


class SignalExtractor:
    """Finds declared features and prospect details in a normalized message."""

    def __init__(self, definition: ProductDefinition) -> None:
        self._topics = tuple(
            (key, _terms(terms)) for key, terms in definition.knowledge_topics.items()
        )
        signals = definition.prospect_signals
        self._roles = _labelled(signals.roles if signals else {})
        self._tools = _labelled(signals.current_tools if signals else {})
        self._goals = _labelled(signals.goals if signals else {})
        self._pains = _labelled(signals.pain_points if signals else {})

    def feature(self, text: NormalizedMessage, subject: Iterable[str] = ()) -> str | None:
        """The topic of the turn's subject if one is declared, else the first topic mentioned.

        `subject` holds the names of what the turn acts on (a view or entity key and its labels).
        A topic claims a subject when one of its terms is one of those names.
        """
        names = {name.lower() for name in subject if name}
        for key, terms in self._topics:
            if names & set(terms):
                return key
        return _first(self._topics, text.focused)

    def profile(self, text: NormalizedMessage) -> ProspectProfile:
        return ProspectProfile(
            role=_first(self._roles, text.focused),
            current_tool=_first(self._tools, text.focused),
            goal=_first(self._goals, text.focused),
            pain_point=_first(self._pains, text.focused),
        )

    def signals(
        self, text: NormalizedMessage, *, feature: str | None = None, person_name: str | None = None,
    ) -> tuple[EngineSignal, ...]:
        found: list[EngineSignal] = []
        if feature:
            found.append(EngineSignal(FEATURE_INTEREST, feature.lower(), FEATURE_CONFIDENCE))
        if pain := self.profile(text).pain_point:
            found.append(EngineSignal(PAIN_POINT, pain.lower(), PAIN_POINT_CONFIDENCE))
        if person_name:
            found.append(EngineSignal(PERSON_INTEREST, person_name, PERSON_CONFIDENCE))
        return tuple(found)


def session_summary(memory: ConversationMemory, history: SignalHistory) -> EngineSummary:
    pending = memory.pending_clarification
    return EngineSummary(
        interests=history.values(FEATURE_INTEREST),
        pain_points=history.values(PAIN_POINT),
        last_person=history.latest_value(PERSON_INTEREST),
        last_feature=history.latest_value(FEATURE_INTEREST),
        clarification_pending=pending.key if pending is not None else None,
    )


def _terms(terms: Iterable[str]) -> tuple[str, ...]:
    # Normalized messages have lost their apostrophes ("i'm" becomes "im"), so terms lose theirs too.
    return tuple(term.lower().replace("'", "").replace("’", "") for term in terms)


def _labelled(groups: Mapping[str, Iterable[str]]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple((label, _terms(terms)) for label, terms in groups.items())


def _first(groups: tuple[tuple[str, tuple[str, ...]], ...], text: str) -> str | None:
    return next((label for label, terms in groups if any(contains_term(text, term) for term in terms)), None)
