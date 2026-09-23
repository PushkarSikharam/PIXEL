"""Deterministic intent routing from the pinned definition (3.2 plan, section 3).

The router turns one message into a `RouteResult` and the conversation memory that follows
from routing alone. Its results are proposals: nothing here validates, authorizes, dispatches
or executes an action. Memory that depends on an accepted action (focus, last person, last
view, a pending confirmation) is updated only by `remember_accepted`, which the validator path
calls once it has approved the proposal.

Platform gates (authorization, pinning, lifecycle) run before the router and are not part of it.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from app.definitions.contract import ActionSpec, EntitySpec, IntentSpec, MatchRule, ProductDefinition
from app.definitions.vocabulary import (
    AFFIRMATIONS,
    CORRECTION_CUES,
    MUTATING_CAPABILITIES,
    PLATFORM_RESPONSE_KEYS,
    Capability,
)
from app.engine.actions import (
    FilterParam,
    GenericAction,
    RecordRef,
    confirmation_reason,
    shape_errors,
)
from app.engine.lookup import PersonView, RecordLookup
from app.engine.conversation import PLATFORM_PHRASES, is_set_aside
from app.engine.memory import ConversationMemory, PendingClarification, PendingConfirmation, PersonFollowUp
from app.engine.mentions import (
    NameMention,
    enum_values,
    name_mentions,
    person_search_words,
    record_ids,
    title_text,
)
from app.engine.normalizer import NormalizedMessage, Normalizer, contains_term
from app.engine.routing import RouteKind, RouteResult, RouteStage
from app.engine.rules import Specificity, exact_match, match_rule, rule_terms

# What each platform clarification waits for. Clarifications not listed ask nothing back.
CLARIFICATION_SLOTS: Mapping[str, str] = MappingProxyType({
    "clarify_owner": "person",
    "clarify_assign": "person",
    "clarify_update_target": "record",
    "clarify_create": "choice",
    "clarify_all_items": "choice",
})
# The platform questions used when a slot is missing or ambiguous and no clarification rule matched.
RECORD_QUESTION = "clarify_update_target"
SLOT_QUESTIONS: Mapping[str, str] = MappingProxyType({
    "record": RECORD_QUESTION, "person": "clarify_assign", "choice": "clarify_change",
})
ORDINALS: Mapping[str, int] = MappingProxyType({
    "first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
})
MAX_CANDIDATES = 3
# Words that reject what follows them in a reply ("not Ana Lopez"), and how far that reaches.
NEGATION_WORDS = frozenset({"not", "except"})
NEGATED_SPAN = 3
# Why an intent could not be completed, strongest first.
_UNMET_PRIORITY = {"ambiguous": 3, "missing": 2, "not_visible": 1}


@dataclass(frozen=True)
class TurnContext:
    turn: int
    # The record the visitor has open, as reported by the client. Re-resolved before use.
    selected: RecordRef | None = None
    # The caller's last executed change in this session and workspace, read from the execution
    # ledger by the caller and passed in as immutable data (5b plan, section 8.4). The router
    # ignores it; only the "what changed?" answer uses it.
    last_change: object | None = None
    # The view the client reports as open. Used only to suggest a next step, and only when it is a
    # view the definition declares.
    view: str | None = None


@dataclass(frozen=True)
class _NamedRecord:
    """What the request said about its target record."""

    ref: RecordRef | None
    named: bool


@dataclass(frozen=True)
class RoutedTurn:
    result: RouteResult
    memory: ConversationMemory


@dataclass(frozen=True)
class _People:
    visible: tuple[PersonView, ...]
    unresolved: tuple[NameMention, ...]


@dataclass(frozen=True)
class _Met:
    intent: IntentSpec
    score: Specificity
    proposal: GenericAction
    person: RecordRef | None
    placeholders: Mapping[str, str]


@dataclass(frozen=True)
class _Unmet:
    intent: IntentSpec
    score: Specificity
    reason: str  # "missing", "not_visible", "ambiguous" or "not_applicable"
    slot: str = "record"
    candidates: tuple[RecordRef, ...] = ()
    name: str | None = None
    target: RecordRef | None = None
    fields: Mapping[str, Any] | None = None


class IntentRouter:
    def __init__(self, definition: ProductDefinition, lookup: RecordLookup) -> None:
        self._definition = definition
        self._lookup = lookup
        rules: list[MatchRule] = [*definition.intents, *definition.clarifications, *definition.guardrails]
        known = rule_terms(rules) | set(definition.vocabulary.terms) | set(definition.vocabulary.corrections.values())
        for entity in definition.entities.values():
            for spec in entity.fields.values():
                known.update(value.lower() for value in spec.values)
        self._known_words = frozenset(word for term in known for word in term.lower().split())
        self.normalizer = Normalizer(
            definition.vocabulary, known,
            protected_phrases=(*AFFIRMATIONS, *CORRECTION_CUES, *ORDINALS, *PLATFORM_PHRASES),
        )
        self._people_entity = definition.people.entity if definition.people else None
        # The focused text of the request being routed, stored with any question it raises.
        self._context_text: str | None = None

    # --- Public ---

    def route(self, message: str, memory: ConversationMemory, context: TurnContext) -> RoutedTurn:
        memory = memory.next_turn(context.turn)
        text = self.normalizer.normalize(message)

        # Stage 1: refusals run on the whole message, before any pending state is completed.
        for rule in self._definition.guardrails:
            if match_rule(rule, text.full) or match_rule(rule, text.focused):
                result = RouteResult(RouteKind.REFUSE, RouteStage.REFUSALS, rule.response, topic=rule.topic)
                return RoutedTurn(result, memory.discard_pending())

        # Stage 2: a pending confirmation is completed only by an explicit affirmation.
        cancelled = False
        if memory.pending_confirmation is not None:
            pending = memory.pending_confirmation
            memory = replace(memory, pending_confirmation=None)
            if text.full in AFFIRMATIONS:
                result = RouteResult(
                    RouteKind.PROPOSE, RouteStage.PENDING_CONFIRMATION, None,
                    proposal=pending.action, confirmed=True,
                )
                return RoutedTurn(result, memory)
            cancelled = True

        # Stage 3: a pending clarification.
        if memory.pending_clarification is not None:
            answered, memory = self._answer_clarification(text, memory, context)
            if answered is not None:
                return _mark_cancelled(answered, cancelled)

        routed = self._route_fresh(text, memory, context)
        if cancelled and routed.result.kind == RouteKind.FALLBACK:
            result = RouteResult(RouteKind.CANCELLED, RouteStage.PENDING_CONFIRMATION, "action_cancelled")
            return RoutedTurn(result, routed.memory)
        return _mark_cancelled(routed, cancelled)

    # --- Stage 3 ---

    def _answer_clarification(
        self, text: NormalizedMessage, memory: ConversationMemory, context: TurnContext,
    ) -> tuple[RoutedTurn | None, ConversationMemory]:
        pending = memory.pending_clarification
        assert pending is not None
        cleared = replace(memory, pending_clarification=None)

        if is_set_aside(text):
            # "Cancel" answers every question the platform asks, not only the create's.
            return RoutedTurn(
                RouteResult(RouteKind.CANCELLED, RouteStage.PENDING_CLARIFICATION, "action_cancelled"),
                cleared,
            ), cleared

        if pending.candidates:
            return self._answer_choice(text, pending, memory, cleared, context), cleared

        if pending.action_key is not None:
            supplied = self._supplied_slot(text, pending)
            if supplied is not None:
                if isinstance(supplied, RouteResult):
                    return RoutedTurn(supplied, cleared), cleared
                return self._complete_pending(pending, supplied, cleared, from_correction=False), cleared

        # A short answer is read together with the request that led to the question
        # ("create something new" + "a contact" continues creating a contact). A complete request
        # of its own is not: "show me how assignment works" after "assign it to Ana?" must not
        # become a request about Ana.
        if pending.context and self._is_complete_request(text, cleared, context):
            return None, cleared
        if pending.context:
            combined = self.normalizer.normalize(f"{pending.context} {text.original}")
            routed = self._route_fresh(combined, cleared, context)
            asked_again = routed.result.kind == RouteKind.CLARIFY and routed.result.response_key == pending.key
            if routed.result.kind != RouteKind.FALLBACK and not asked_again:
                return routed, cleared

        if self._matches_any_rule(text):
            return None, cleared  # a new request replaces the question
        return self._ask_again(pending, memory, context), cleared

    def _is_complete_request(self, text: NormalizedMessage, memory: ConversationMemory, context: TurnContext) -> bool:
        """A message of several words that routes to an action or a refusal entirely on its own."""
        if len(text.full.split()) < 3:
            return False
        alone = self._route_fresh(text, memory, context).result
        return alone.kind in (RouteKind.PROPOSE, RouteKind.CONFIRM, RouteKind.REFUSE)

    def _answer_choice(
        self, text: NormalizedMessage, pending: PendingClarification, memory: ConversationMemory,
        cleared: ConversationMemory, context: TurnContext,
    ) -> RoutedTurn | None:
        chosen, rejected = self._candidate_reply(text, pending)
        if chosen is not None:
            return self._complete_pending(pending, chosen, cleared, from_correction=False)
        if rejected:
            return self._after_rejection(pending, rejected, cleared, context)
        if self._has_correction_cue(text):
            if pending.singled_out is None:
                # "Not that one" after a list: ask which one is meant instead of guessing.
                return self._ask_again(pending, memory, context)
            return self._after_rejection(pending, (pending.singled_out,), cleared, context)
        if self._matches_any_rule(text):
            return None  # a new request replaces the question
        return self._ask_again(pending, memory, context)

    def _after_rejection(
        self, pending: PendingClarification, rejected: tuple[RecordRef, ...], cleared: ConversationMemory,
        context: TurnContext,
    ) -> RoutedTurn:
        """Remove rejected candidates and ask again. A correction never picks on the visitor's behalf."""
        corrected = pending.reject(rejected)
        remaining = tuple(ref for ref in corrected.candidates if self._visible(ref))
        corrected = replace(corrected, candidates=remaining, turn=context.turn)
        if len(remaining) == 1 and pending.action_key is not None:
            spec = self._definition.actions[pending.action_key]
            if spec.capability in MUTATING_CAPABILITIES:
                # Completed only after an explicit yes (confirmation reason: correction).
                return self._complete_pending(corrected, remaining[0], cleared, from_correction=True)
            # Name the remaining candidate and ask; never act on it directly.
            corrected = replace(corrected, singled_out=remaining[0])
        result = RouteResult(RouteKind.CLARIFY, RouteStage.PENDING_CLARIFICATION, pending.key)
        return RoutedTurn(result, replace(cleared, pending_clarification=corrected))

    def _ask_again(
        self, pending: PendingClarification, memory: ConversationMemory, context: TurnContext,
    ) -> RoutedTurn | None:
        again = pending.repeated(context.turn)
        if again is None:
            return None
        result = RouteResult(RouteKind.CLARIFY, RouteStage.PENDING_CLARIFICATION, pending.key)
        return RoutedTurn(result, replace(memory, pending_clarification=again))

    def _candidate_reply(
        self, text: NormalizedMessage, pending: PendingClarification,
    ) -> tuple[RecordRef | None, tuple[RecordRef, ...]]:
        """The candidate a reply selects, and the candidates it explicitly rejects ("not Ana Lopez")."""
        candidates = [ref for ref in pending.candidates if self._visible(ref)]
        if not candidates:
            return None, ()
        words = text.words
        negated: set[str] = set()
        for index, word in enumerate(words):
            if word in NEGATION_WORDS:
                negated.update(words[index + 1:index + 1 + NEGATED_SPAN])
        labels: dict[RecordRef, set[str]] = {}
        for ref in candidates:
            record = self._lookup.get(ref.entity, ref.id)
            labels[ref] = {ref.id.lower()} | (set(record.title.lower().split()) if record else set())

        rejected: tuple[RecordRef, ...] = ()
        if negated:
            hits = {ref: len(negated & labels[ref]) for ref in candidates}
            strongest = max(hits.values())
            rejected = tuple(ref for ref, count in hits.items() if strongest and count == strongest)
            if len(rejected) == len(candidates):
                rejected = ()  # "not Ana" with only Anas says nothing about which one

        affirmed = [word for word in words if word not in negated]
        # Collect every ordinal the reply uses. "first or second" points at two candidates, so
        # it selects neither: the question is asked again.
        chosen = [
            candidates[ORDINALS[word]] for word in dict.fromkeys(affirmed)
            if word in ORDINALS and ORDINALS[word] < len(candidates)
        ]
        if len(chosen) == 1:
            return chosen[0], ()
        if chosen:
            return None, rejected
        overlap = {ref: len(set(affirmed) & labels[ref]) for ref in candidates if ref not in rejected}
        if overlap:
            best = max(overlap.values())
            named = [ref for ref, count in overlap.items() if count == best]
            # Only an answer that fits one candidate better than every other selects it.
            if best > 0 and len(named) == 1:
                return named[0], ()
        return None, rejected

    def _has_correction_cue(self, text: NormalizedMessage) -> bool:
        words = text.words
        if words and words[0] in CORRECTION_CUES:
            return True
        return any(" " in cue and contains_term(text.full, cue) for cue in CORRECTION_CUES)

    def _supplied_slot(self, text: NormalizedMessage, pending: PendingClarification) -> RecordRef | RouteResult | None:
        if pending.expected == "person" and self._people_entity is not None:
            # The whole answer is the name here, including its first word.
            people = self._people(text, answers_with_a_name=True)
            if len(people.visible) == 1:
                return RecordRef(self._people_entity, people.visible[0].id)
            if people.unresolved and not people.visible:
                # The same answer a request naming an unknown person gets: say so, and offer the
                # control that adds one when the product declares it.
                return self._person_not_found(people.unresolved[0].text, RouteStage.PENDING_CLARIFICATION)
        if pending.expected == "record" and pending.action_key is not None:
            entity = self._definition.actions[pending.action_key].entity
            for record_id in record_ids(text.words):
                if entity and self._lookup.get(entity, record_id):
                    return RecordRef(entity, record_id)
        return None

    def _complete_pending(
        self, pending: PendingClarification, chosen: RecordRef, memory: ConversationMemory, *, from_correction: bool,
    ) -> RoutedTurn:
        assert pending.action_key is not None
        spec = self._definition.actions[pending.action_key]
        fields = dict(pending.fields or {})
        target = pending.target
        person: RecordRef | None = None
        if chosen.entity == self._people_entity and chosen.entity != spec.entity:
            person = chosen
            people_fields = self._people_fields(spec)
            if spec.capability == Capability.FILTER_RECORDS:
                return self._proposal_turn(spec, pending.action_key, memory, from_correction,
                                           filter=FilterParam(spec.by, chosen.id), person=person)
            if people_fields:
                fields[people_fields[0]] = chosen.id
        else:
            target = chosen
        params: dict[str, Any] = {}
        if spec.capability in (Capability.OPEN_RECORD, Capability.UPDATE_RECORD) or spec.record:
            params["target"] = target
        if spec.capability in MUTATING_CAPABILITIES:
            params["fields"] = fields
        if spec.capability == Capability.HIGHLIGHT_CONTROL:
            params.update(view=spec.view, control=spec.control)
        if params.get("target") is None and "target" in params:
            result = RouteResult(RouteKind.CLARIFY, RouteStage.PENDING_CLARIFICATION, RECORD_QUESTION)
            again = replace(pending, candidates=(), singled_out=None, fields=fields or None)
            return RoutedTurn(result, replace(memory, pending_clarification=again))
        if spec.capability in MUTATING_CAPABILITIES and not fields:
            # The record is known but nothing was named to change: ask, never guess, and never
            # build a proposal the contract would reject.
            key = SLOT_QUESTIONS["choice"]
            asked = PendingClarification(key, pending.action_key, "choice", target=target,
                                         turn=pending.turn, context=pending.context)
            result = RouteResult(RouteKind.CLARIFY, RouteStage.PENDING_CLARIFICATION, key,
                                 placeholders=MappingProxyType({"record_id": target.id if target else ""}))
            return RoutedTurn(result, replace(memory, pending_clarification=asked))
        return self._proposal_turn(spec, pending.action_key, memory, from_correction, person=person, **params)

    def _proposal_turn(
        self, spec: ActionSpec, action_key: str, memory: ConversationMemory, from_correction: bool,
        person: RecordRef | None = None, **params: Any,
    ) -> RoutedTurn:
        proposal = self._build(action_key, **params)
        reason = confirmation_reason(spec, target_from_correction=from_correction)
        if reason is not None:
            result = RouteResult(RouteKind.CONFIRM, RouteStage.PENDING_CLARIFICATION, "confirm_action",
                                 proposal=proposal, confirmation_reason=reason, person=person)
        else:
            result = RouteResult(RouteKind.PROPOSE, RouteStage.PENDING_CLARIFICATION, None,
                                 proposal=proposal, person=person)
        return RoutedTurn(result, memory)

    # --- Stages 4 to 9 ---

    def _route_fresh(self, text: NormalizedMessage, memory: ConversationMemory, context: TurnContext) -> RoutedTurn:
        self._context_text = text.focused
        people = self._people(text)
        met: list[_Met] = []
        unmet: list[_Unmet] = []
        for intent in self._definition.intents:
            score = match_rule(intent, text.focused)
            if score is None:
                continue
            outcome = self._evaluate(intent, score, text, people, memory, context)
            (met if isinstance(outcome, _Met) else unmet).append(outcome)
        clarification = self._first_clarification(text)

        best = max((m.score for m in met), default=None)
        leaders = [m for m in met if m.score == best]

        # Stage 4 covers every exact phrase. An exact clarification therefore beats a grouped
        # intent, and an exact clarification competing with an exact intent is an ambiguity
        # that file order must not resolve.
        exact_clarifications = self._exact_clarifications(text)
        if exact_clarifications:
            exact_leaders = [m for m in leaders if m.score.exact == 1]
            responses = {rule.response for rule in exact_clarifications}
            if exact_leaders or len(responses) > 1:
                return RoutedTurn(RouteResult(RouteKind.FALLBACK, RouteStage.FALLBACK, "fallback"), memory)
            return self._clarify_or_fallback(
                exact_clarifications[0], RouteStage.EXACT_PHRASES, memory, context
            )
        blocking = [
            u for u in unmet
            if u.reason != "not_applicable" and (best is None or u.score > best)
        ]
        blocking.sort(key=lambda u: (_UNMET_PRIORITY[u.reason], u.score), reverse=True)
        for failure in blocking:
            handled = self._handle_unmet(failure, leaders, clarification, memory, context)
            if handled is not None:
                return handled

        if leaders:
            if not _all_same(leaders):
                # A tie is never broken by file order.
                return self._clarify_or_fallback(clarification, RouteStage.INTENT_GROUPS, memory, context)
            unseen = next((u for u in unmet if u.reason == "not_visible" and u.slot == "person"), None)
            if unseen is not None and leaders[0].person is None and "person" not in leaders[0].placeholders:
                # The request named someone who is not available and the chosen action goes ahead
                # without them ("open Zed's record" opens the list): say so first. Unknown and
                # inaccessible people read the same.
                return self._propose(leaders[0], memory, override=("unknown_person", {"person": unseen.name or ""}))
            return self._propose(leaders[0], memory, override=None)

        if clarification is None:
            follow_up = self._person_follow_up(text, people, memory, context)
            if follow_up is not None:
                return follow_up
            named = self._named_record(text)
            if named is not None:
                return RoutedTurn(
                    RouteResult(RouteKind.PROPOSE, RouteStage.INTENT_GROUPS, "record_opened",
                                proposal=named),
                    memory,
                )
        return self._clarify_or_fallback(clarification, RouteStage.CLARIFICATION_RULES, memory, context)

    def _named_record(self, text: NormalizedMessage) -> GenericAction | None:
        """A visible record the message names by its identifier ("open ACC-1", or just "ACC-1").

        Only when nothing else matched, and only for an entity this product declares an action to
        open. The identifier still goes through the caller's own lookup, so naming a record nobody
        may see finds nothing.
        """
        for key, spec in sorted(self._definition.actions.items()):
            if spec.capability != Capability.OPEN_RECORD or spec.entity is None:
                continue
            for record_id in record_ids(text.words):
                if self._lookup.get(spec.entity, record_id) is None:
                    continue
                return self._build(key, target=RecordRef(spec.entity, record_id))
        return None

    def _person_follow_up(
        self, text: NormalizedMessage, people: _People, memory: ConversationMemory, context: TurnContext,
    ) -> RoutedTurn | None:
        """"What about <person>" re-applies the previous request to that person (5a plan, 4.5).

        Only when nothing else matched, only on the turn right after that request was accepted,
        and only for a message naming exactly one person. A person-based request is re-applied
        as it was; any other request's subject is filtered by the person, through the one person
        filter the definition declares for that entity (none, or more than one, means no guess).
        The person is resolved exactly as any other request resolves it, so someone unknown or
        out of scope gets `unknown_person`.
        """
        last = memory.person_follow_up
        if last is None or last.turn != context.turn - 1:
            return None
        # Here the word after "about" is the person, so it counts as a name even when nobody by
        # that name is visible; otherwise "what about Cara" and "Ben and Cara" would lose Cara.
        people = self._people(text, subjects_are_names=True)
        if len(people.visible) + len(people.unresolved) != 1:
            return None
        intent = next(
            (item for item in self._definition.intents
             if item.action == last.action_key and "person" in item.requires),
            None,
        ) or self._person_filter_for(last.action_key)
        if intent is None:
            return None
        outcome = self._evaluate(intent, Specificity(0, 0, 0, 0, 0), text, people, memory, context)
        if isinstance(outcome, _Met):
            return self._propose(outcome, memory, override=None)
        if outcome.reason == "not_visible" and outcome.slot == "person":
            result = RouteResult(RouteKind.ANSWER, RouteStage.INTENT_GROUPS, "unknown_person",
                                 placeholders={"person": outcome.name or ""})
            return RoutedTurn(result, memory)
        return None

    def _person_filter_for(self, action_key: str) -> IntentSpec | None:
        """The single person-filter intent over the entity `action_key` was about, if exactly one."""
        spec = self._definition.actions.get(action_key)
        if spec is None:
            return None
        entity = spec.entity
        if entity is None and spec.view is not None and spec.view in self._definition.views:
            entity = self._definition.views[spec.view].entity
        if entity is None:
            return None
        filters = [
            item for item in self._definition.intents
            if "person" in item.requires
            and self._definition.actions[item.action].capability == Capability.FILTER_RECORDS
            and self._definition.actions[item.action].entity == entity
        ]
        if len({item.action for item in filters}) != 1:
            return None
        return filters[0]

    def _handle_unmet(
        self, failure: _Unmet, leaders: list[_Met], clarification: MatchRule | None,
        memory: ConversationMemory, context: TurnContext,
    ) -> RoutedTurn | None:
        stage = RouteStage.REQUIREMENTS
        if failure.reason == "ambiguous":
            # The question must be about the ambiguity itself; without one, the router does not act.
            key = self._question_for(failure.slot, failure.intent.action)
            if key is None:
                return RoutedTurn(RouteResult(RouteKind.FALLBACK, RouteStage.FALLBACK, "fallback"), memory)
            pending = PendingClarification(
                key, failure.intent.action, "choice", candidates=failure.candidates[:MAX_CANDIDATES],
                target=failure.target, fields=failure.fields, turn=context.turn, context=self._context_text,
            )
            result = RouteResult(RouteKind.CLARIFY, stage, key)
            return RoutedTurn(result, replace(memory, pending_clarification=pending))
        if failure.reason == "missing":
            key = clarification.response if clarification else None
            if key is None and failure.slot == "person" and not leaders and (
                self._definition.actions[failure.intent.action].capability in MUTATING_CAPABILITIES
            ):
                # A change request that named no person the platform recognises ("assign it to
                # priya" in lower case) asks who is meant. Without this it falls through to
                # "I'm not sure how to help", which is not true: the request was understood.
                key = self._question_for(failure.slot, failure.intent.action)
            if key is None and failure.slot in ("record", "choice"):
                key = self._question_for(failure.slot, failure.intent.action)
            if key is None:
                return None
            slot = CLARIFICATION_SLOTS.get(key, failure.slot)
            pending = PendingClarification(
                key, failure.intent.action, slot, target=failure.target, fields=failure.fields, turn=context.turn,
                context=self._context_text,
            )
            return RoutedTurn(RouteResult(RouteKind.CLARIFY, stage, key), replace(memory, pending_clarification=pending))
        if failure.slot == "record":
            # A record the visitor named but cannot have: ask which one, and never fall back to
            # a remembered record. Unavailable and nonexistent get the same answer.
            key = self._question_for("record", failure.intent.action)
            if key is None:
                return RoutedTurn(RouteResult(RouteKind.FALLBACK, RouteStage.FALLBACK, "fallback"), memory)
            pending = PendingClarification(
                key, failure.intent.action, CLARIFICATION_SLOTS.get(key, "record"),
                fields=failure.fields, turn=context.turn, context=self._context_text,
            )
            return RoutedTurn(RouteResult(RouteKind.CLARIFY, stage, key),
                              replace(memory, pending_clarification=pending))
        # Not visible: identical to unknown. Never say the person exists elsewhere.
        placeholders = {"person": failure.name or ""}
        if leaders and _all_same(leaders):
            return self._propose(leaders[0], memory, override=("unknown_person", placeholders))
        return RoutedTurn(self._person_not_found(failure.name or "", stage), memory)

    def _clarify_or_fallback(
        self, clarification: MatchRule | None, stage: RouteStage, memory: ConversationMemory, context: TurnContext,
    ) -> RoutedTurn:
        if clarification is None:
            return RoutedTurn(RouteResult(RouteKind.FALLBACK, RouteStage.FALLBACK, "fallback"), memory)
        key = clarification.response
        slot = CLARIFICATION_SLOTS.get(key)
        if slot is not None:
            memory = replace(memory, pending_clarification=PendingClarification(
                key, None, slot, turn=context.turn, context=self._context_text,
            ))
        return RoutedTurn(RouteResult(RouteKind.CLARIFY, stage, key), memory)

    def _propose(self, met: _Met, memory: ConversationMemory, override: tuple[str, Mapping[str, str]] | None) -> RoutedTurn:
        spec = self._definition.actions[met.intent.action]
        response = met.intent.response
        placeholders = dict(met.placeholders)
        if override is not None:
            response = override[0]
            placeholders.update(override[1])
        stage = RouteStage.EXACT_PHRASES if met.score.exact else RouteStage.INTENT_GROUPS
        creating = self._create_for(spec) if response == "clarify_owner" else None
        if creating is not None:
            memory = replace(memory, pending_clarification=PendingClarification(
                response, creating, "person", turn=memory.turn, context=self._context_text,
            ))
        reason = confirmation_reason(spec, target_from_correction=False)
        if reason is not None:
            result = RouteResult(RouteKind.CONFIRM, stage, "confirm_action", proposal=met.proposal,
                                 confirmation_reason=reason, person=met.person, placeholders=placeholders)
        else:
            result = RouteResult(RouteKind.PROPOSE, stage, response, proposal=met.proposal,
                                 person=met.person, placeholders=placeholders)
        return RoutedTurn(result, memory)

    # --- Intent evaluation ---

    def _evaluate(
        self, intent: IntentSpec, score: Specificity, text: NormalizedMessage, people: _People,
        memory: ConversationMemory, context: TurnContext,
    ) -> _Met | _Unmet:
        spec = self._definition.actions[intent.action]
        entity = self._definition.entities.get(spec.entity) if spec.entity else None
        requires = set(intent.requires)
        satisfied = 0
        placeholders: dict[str, str] = {}
        prefill: dict[str, Any] = {}
        person: PersonView | None = None
        target: RecordRef | None = None

        def unmet(reason: str, **details: Any) -> _Unmet:
            return _Unmet(intent, replace(score, requirements=satisfied), reason, **details)

        if "unknown_person" in requires:
            # A known person named alongside does not make the unknown one someone else: nothing is
            # created or assigned for a person who is not in the directory, whoever else is named.
            if not people.unresolved:
                return unmet("not_applicable")
            name = people.unresolved[0]
            placeholders["person"] = name.text
            if entity is not None and entity.title_field in spec.prefill:
                prefill[entity.title_field] = name.text
            satisfied += 1

        if "record" in requires or "selected_record" in requires:
            named = self._explicit_record(spec, text) if "record" in requires else _NamedRecord(None, False)
            if named.ref is not None:
                target = named.ref
            elif named.named:
                # The visitor named a record that is not available. Asking is the only safe
                # answer: acting on a remembered record would change something else entirely.
                return unmet("not_visible", slot="record")
            else:
                target = self._current_record(spec.entity, memory, context)
                if target is None:
                    return unmet("missing", slot="record",
                                 fields=self._changed_fields(spec, entity, text) or None)
            satisfied += 1

        people_needed = "person" in requires or (
            spec.capability in (Capability.FILTER_RECORDS, Capability.UPDATE_RECORD) and self._people_fields(spec)
        )
        if people_needed:
            if len(people.visible) > 1:
                return unmet("ambiguous", slot="person", candidates=self._person_refs(people.visible), target=target)
            if people.visible and people.unresolved and spec.capability == Capability.CREATE_RECORD:
                # "A record for A, assigned to B" with B unknown: drafting it around A alone would
                # drop half the request. The unknown person is dealt with first.
                return unmet("not_visible", slot="person", name=people.unresolved[0].text)
            if people.visible:
                person = people.visible[0]
                placeholders["person"] = person.name
                satisfied += "person" in requires
            elif "person" in requires:
                if people.unresolved:
                    return unmet("not_visible", slot="person", name=people.unresolved[0].text)
                return unmet("missing", slot="person")

        person_ref = RecordRef(self._people_entity, person.id) if person and self._people_entity else None
        capability = spec.capability
        params: dict[str, Any] = {}
        if capability == Capability.NAVIGATE_VIEW:
            params["view"] = spec.view
        elif capability == Capability.OPEN_RECORD:
            if target is None and person is not None:
                records = self._lookup.by_person(spec.entity, person.id, MAX_CANDIDATES + 1)
                if len(records) > 1:
                    refs = tuple(RecordRef(r.entity, r.id) for r in records)
                    return unmet("ambiguous", slot="record", candidates=refs)
                if not records:
                    return unmet("missing", slot="record")
                target = RecordRef(records[0].entity, records[0].id)
            if target is None:
                return unmet("missing", slot="record")
            params["target"] = target
            placeholders["record_id"] = target.id
        elif capability == Capability.FILTER_RECORDS:
            if person is None:
                if people.unresolved:
                    return unmet("not_visible", slot="person", name=people.unresolved[0].text)
                return unmet("missing", slot="person")
            params["filter"] = FilterParam(spec.by, person.id)
        elif capability == Capability.CREATE_RECORD:
            fields: dict[str, Any] = {}
            people_fields = self._people_fields(spec)
            if person is not None and people_fields:
                fields[people_fields[0]] = person.id
            title = title_text(text.original)
            if entity is not None and title and entity.title_field in spec.fields:
                fields[entity.title_field] = title
            if not fields:
                return unmet("missing", slot="person" if people_fields else "choice")
            params["fields"] = fields
        elif capability == Capability.UPDATE_RECORD:
            fields = {}
            people_fields = self._people_fields(spec)
            if person is not None and people_fields:
                fields[people_fields[0]] = person.id
            named_values = enum_values(entity, list(spec.fields), text.focused) if entity else {}
            for name, values in named_values.items():
                if len(values) > 1:
                    return unmet("ambiguous", slot="choice", target=target)
                fields[name] = values[0]
            if not fields:
                # Which slot is missing depends on what the message named. A request that names a
                # declared field with a value this product does not declare ("set the urgency to
                # bananas") is about that field, so its unrecognised word is never read as a
                # person; anything else that names someone, or names the people field, is.
                values_field = self._mentions_field(
                    entity, [name for name in spec.fields if name not in people_fields], text)
                wants_person = bool(people_fields) and not values_field
                if people.unresolved and wants_person:
                    return unmet("not_visible", slot="person", name=people.unresolved[0].text, target=target)
                asks_person = wants_person and self._mentions_field(entity, people_fields, text)
                return unmet("missing", slot="person" if asks_person else "choice", target=target)
            params.update(target=target, fields=fields)
            placeholders["record_id"] = target.id if target else ""
        elif capability == Capability.HIGHLIGHT_CONTROL:
            params.update(view=spec.view, control=spec.control)
            if spec.record:
                target = target or self._current_record(spec.entity, memory, context)
                if target is None:
                    return unmet("missing", slot="record")
                params["target"] = target
            if prefill:
                params["prefill"] = prefill

        proposal = self._build(intent.action, **params)
        return _Met(intent, replace(score, requirements=satisfied), proposal, person_ref, MappingProxyType(placeholders))

    # --- Helpers ---

    def _build(self, action_key: str, **params: Any) -> GenericAction:
        proposal = GenericAction.for_definition(self._definition, action_key, **params)
        errors = shape_errors(proposal, self._definition)
        if errors:
            # A router bug, never a visitor error: proposals must at least be well formed.
            raise ValueError(f"router built a malformed {action_key} proposal: {errors}")
        return proposal

    def _people(self, text: NormalizedMessage, *, subjects_are_names: bool = False,
                answers_with_a_name: bool = False) -> _People:
        found: dict[str, PersonView] = {}
        if self._people_entity is not None:
            for word in person_search_words(tuple(text.focused.split()), self._known_words):
                for person in self._lookup.people(word, MAX_CANDIDATES).matches:
                    found.setdefault(person.id, person)
        focused_words = set(text.focused.split())
        mentions = [
            mention for mention in name_mentions(
                text.original, self._known_words, subjects_are_names=subjects_are_names,
                first_word_is_name=answers_with_a_name)
            if set(mention.words) & focused_words
        ]
        visible_words = {word for person in found.values() for word in person.name.lower().split()}
        unresolved = tuple(mention for mention in mentions if not set(mention.words) & visible_words)
        return _People(tuple(found.values()), unresolved)

    def _person_refs(self, people: tuple[PersonView, ...]) -> tuple[RecordRef, ...]:
        assert self._people_entity is not None
        return tuple(RecordRef(self._people_entity, person.id) for person in people)

    def _person_not_found(self, name: str, stage: RouteStage) -> RouteResult:
        """One answer for a person this caller cannot see, whether or not they exist elsewhere.

        When the product declares a control that adds a person, it is offered with the name the
        visitor typed. Nothing is created: the visitor adds the person themselves.
        """
        placeholders = MappingProxyType({"person": name})
        adding = self._add_person_action()
        if adding is None or not name:
            return RouteResult(RouteKind.ANSWER, stage, "unknown_person", placeholders=placeholders)
        spec = self._definition.actions[adding]
        entity = self._definition.entities[spec.entity]
        proposal = self._build(adding, view=spec.view, control=spec.control,
                               prefill={entity.title_field: name})
        return RouteResult(RouteKind.PROPOSE, stage, "member_missing", proposal=proposal,
                           placeholders=placeholders)

    def _add_person_action(self) -> str | None:
        """The action that shows where a person is added, if the definition declares exactly one."""
        if self._people_entity is None:
            return None
        entity = self._definition.entities[self._people_entity]
        found = [
            key for key, spec in sorted(self._definition.actions.items())
            if spec.capability == Capability.HIGHLIGHT_CONTROL and spec.entity == self._people_entity
            and list(spec.prefill) == [entity.title_field]
        ]
        return found[0] if len(found) == 1 else None

    def _create_for(self, spec: ActionSpec) -> str | None:
        """The action that creates the kind of record this one is about, if there is exactly one."""
        entity = spec.entity or (self._definition.views[spec.view].entity if spec.view else None)
        found = [
            key for key, candidate in sorted(self._definition.actions.items())
            if candidate.capability == Capability.CREATE_RECORD and candidate.entity == entity
        ]
        return found[0] if len(found) == 1 else None

    def _changed_fields(self, spec: ActionSpec, entity: EntitySpec | None, text: NormalizedMessage) -> dict:
        """The values a change request names, read the same way whether or not its record is known."""
        if entity is None or spec.capability != Capability.UPDATE_RECORD:
            return {}
        found = {}
        for name, values in enum_values(entity, list(spec.fields), text.focused).items():
            if len(values) == 1:
                found[name] = values[0]
        return found

    def _mentions_field(self, entity: EntitySpec | None, names: list[str], text: NormalizedMessage) -> bool:
        """Whether the message names one of these fields, by its key or its declared label."""
        if entity is None:
            return False
        for name in names:
            spec = entity.fields.get(name)
            words = {name.replace("_", " "), (spec.label or name).lower() if spec else name}
            stem = name.replace("_", " ")[:6]
            if any(contains_term(text.full, word) for word in words) or stem in text.full:
                return True
        return False

    def _people_fields(self, spec: ActionSpec) -> list[str]:
        if self._people_entity is None or spec.entity is None:
            return []
        entity: EntitySpec = self._definition.entities[spec.entity]
        names = list(spec.fields) or ([spec.by] if spec.by else [])
        return [
            name for name in names
            if name in entity.fields and entity.fields[name].type == "ref"
            and entity.fields[name].target == self._people_entity
        ]

    def _explicit_record(self, spec: ActionSpec, text: NormalizedMessage) -> _NamedRecord:
        """The record the corrected request names, if any, and whether it named one at all.

        Reading the corrected clause matters: in "status CON-1 Closed, actually status CON-2
        Closed" the request is about CON-2. Naming a record the caller cannot see is a different
        answer from naming none, because a remembered record must never be substituted for it.
        """
        if spec.entity is None:
            return _NamedRecord(None, False)
        named = record_ids(text.focused_words) or record_ids(text.words)
        for record_id in named:
            if self._lookup.get(spec.entity, record_id) is not None:
                return _NamedRecord(RecordRef(spec.entity, record_id), True)
        return _NamedRecord(None, bool(named))

    def _current_record(self, entity: str | None, memory: ConversationMemory, context: TurnContext) -> RecordRef | None:
        for ref in (memory.focus, context.selected):
            if ref is not None and ref.entity == entity and self._visible(ref):
                return ref
        return None

    def _visible(self, ref: RecordRef) -> bool:
        return self._lookup.get(ref.entity, ref.id) is not None

    def _question_for(self, slot: str, action_key: str) -> str | None:
        """The platform question for a slot, suited to the action that needs it."""
        capability = self._definition.actions[action_key].capability
        if slot == "person":
            key = "clarify_assign" if capability in MUTATING_CAPABILITIES else "clarify_person"
        else:
            key = SLOT_QUESTIONS.get(slot)
        # Slot questions are platform wording, so they never depend on a definition declaring them.
        return key if key in PLATFORM_RESPONSE_KEYS or key in self._definition.responses else None

    def _matches_any_rule(self, text: NormalizedMessage) -> bool:
        rules: list[MatchRule] = [*self._definition.intents, *self._definition.clarifications]
        return any(match_rule(rule, text.focused) for rule in rules)

    def _first_clarification(self, text: NormalizedMessage) -> MatchRule | None:
        return next((rule for rule in self._definition.clarifications if match_rule(rule, text.focused)), None)

    def _exact_clarifications(self, text: NormalizedMessage) -> list[MatchRule]:
        """Clarification rules whose exact phrase is the whole corrected request."""
        return [rule for rule in self._definition.clarifications if exact_match(rule, text.focused)]


def remember_accepted(memory: ConversationMemory, result: RouteResult) -> ConversationMemory:
    """Memory after the validator has accepted `result`'s proposal.

    Router output alone never calls this; it is the validator path's job (slice 3). A proposal
    that needs confirmation is stored as pending, with its reason, instead of being remembered
    as done.
    """
    proposal = result.proposal
    if proposal is None:
        return memory
    if result.kind == RouteKind.CONFIRM:
        assert result.confirmation_reason is not None
        pending = PendingConfirmation(proposal, result.confirmation_reason, memory.turn)
        return replace(memory, pending_confirmation=pending)
    updates: dict[str, Any] = {}
    if proposal.target is not None:
        updates["focus"] = proposal.target
    if result.person is not None:
        updates["last_person"] = result.person
    # Any accepted request can be followed by "what about <person>" on the next turn.
    updates["person_follow_up"] = PersonFollowUp(proposal.action_key, memory.turn)
    if proposal.capability == Capability.NAVIGATE_VIEW:
        updates["last_view"] = proposal.view
    return replace(memory, **updates)


def _all_same(leaders: list[_Met]) -> bool:
    """Whether equally ranked intents all propose exactly the same thing (proposals are not hashable)."""
    first = leaders[0]
    return all((m.proposal, m.person) == (first.proposal, first.person) for m in leaders[1:])


def _mark_cancelled(routed: RoutedTurn, cancelled: bool) -> RoutedTurn:
    if not cancelled:
        return routed
    return RoutedTurn(replace(routed.result, cancelled_confirmation=True), routed.memory)
