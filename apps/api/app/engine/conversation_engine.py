"""The conversation engine: one turn, as a pure function of its inputs (5a plan, section 4.1).

    EngineTurn = ConversationEngine(definition, snapshot, capability_policy, knowledge)
                   .turn(message, memory, history, context)

Every input is immutable and the output is a new value; `turn()` reads nothing else. The engine
is given no database connection, no execution ledger, no model transport and no speech service,
so there is nothing to switch off: it cannot call a provider, write a record or execute an action.
A mutation stops at "proposed" or "awaiting confirmation". A "yes" to a pending confirmation is
recorded as `WOULD_EXECUTE` and worded as the proposal, never as a completed change.

Turn order (normative for 5a):

1. Normalize and route with `IntentRouter` and the prior memory (refusals, pending confirmation,
   pending clarification, exact phrases, intent groups, requirements, clarification rules).
2. A proposal is validated against the snapshot. A refusal is `REFUSED`.
3. Only when routing fell back: platform conversation (greeting, identity, capabilities), then
   knowledge for a question, then the platform fallback. These come after routing so they can
   never pre-empt a refusal, a pending question or a real request.
4. Every sentence is composed by `ResponseComposer` from platform wording.
5. A validated action is translated by the product's registered translator, for comparison only.
6. Signals and the session summary come from the message, the next memory and the next history.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.definitions.contract import ProductDefinition
from app.engine.actions import GenericAction
from app.engine.composer import Reply, ResponseComposer, describe_changes
from app.engine.conversation import (
    KNOWLEDGE_UNAVAILABLE_KEY,
    CapabilityPolicy,
    Conversational,
    detect,
    offerable,
)
from app.engine.knowledge import KnowledgeLookup, KnowledgePassage, ground
from app.engine.memory import ConversationMemory
from app.engine.normalizer import NormalizedMessage
from app.engine.router import IntentRouter, TurnContext, remember_accepted
from app.engine.routing import RouteKind, RouteResult
from app.engine.signals import (
    EngineSignal,
    EngineSummary,
    ProspectProfile,
    SignalExtractor,
    SignalHistory,
    session_summary,
)
from app.engine.snapshot import TurnSnapshot
from app.engine.validator import ActionContractValidator, Refusal, ValidatedAction

# The platform's name for the caller's scope in replies. Scope identifiers are evidence, never speech.
SCOPE_NAME = "this workspace"

# A message is a question, and so may be answered from knowledge, when it ends with a question
# mark or opens with one of these words. Anything else that no stage handled gets the fallback.
QUESTION_OPENERS = frozenset({
    "how", "what", "why", "when", "where", "which", "who", "can", "could", "does", "do", "is",
    "are", "should", "tell", "explain",
})

# Validator refusal codes and the platform reply each one gets. Unknown and inaccessible records
# read the same; any other code is a request the platform could not carry out as asked.
REFUSAL_REPLIES = {
    "record_not_found": "work_outside_scope",
    "reference_not_found": "work_outside_scope",
}


class TurnStage(StrEnum):
    PROPOSED = "proposed"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    WOULD_EXECUTE = "would_execute"  # an affirmed confirmation; nothing executes in 5a
    REFUSED = "refused"
    CLARIFICATION = "clarification"
    ANSWER = "answer"
    KNOWLEDGE = "knowledge"
    UNGROUNDED = "ungrounded"
    CANCELLED = "cancelled"
    FALLBACK = "fallback"


# Stages that carry a validated action.
ACTION_STAGES = frozenset({TurnStage.PROPOSED, TurnStage.AWAITING_CONFIRMATION, TurnStage.WOULD_EXECUTE})


@dataclass(frozen=True)
class EngineTurn:
    stage: TurnStage
    route: RouteResult
    validated: ValidatedAction | None
    refusal: Refusal | None
    reply: Reply
    # The product translator's legacy form of the validated action, for comparison only.
    legacy_action: Any | None
    translation_missing: str | None
    feature: str | None
    profile: ProspectProfile
    signals: tuple[EngineSignal, ...]
    passages: tuple[KnowledgePassage, ...]
    summary: EngineSummary
    memory: ConversationMemory
    history: SignalHistory


class ConversationEngine:
    def __init__(
        self,
        definition: ProductDefinition,
        snapshot: TurnSnapshot,
        capability_policy: CapabilityPolicy,
        knowledge: KnowledgeLookup | None = None,
        *,
        definition_version: int,
        translate: Callable[[ValidatedAction], Any] | None = None,
    ) -> None:
        self._definition = definition
        self._snapshot = snapshot
        self._policy = capability_policy
        self._knowledge = knowledge
        self._version = definition_version
        self._translate = translate
        self._signals = SignalExtractor(definition)

    def turn(
        self,
        message: str,
        memory: ConversationMemory,
        history: SignalHistory,
        context: TurnContext,
    ) -> EngineTurn:
        # A router per turn: it keeps working state while routing, and turns never share it.
        router = IntentRouter(self._definition, self._snapshot)
        routed = router.route(message, memory, context)
        text = router.normalizer.normalize(message)
        result, next_memory = routed.result, routed.memory
        composer = ResponseComposer(self._definition)
        validated: ValidatedAction | None = None
        refusal: Refusal | None = None
        passages: tuple[KnowledgePassage, ...] = ()
        # Whether documents are retrieved as evidence for this reply (never spoken). A refusal, a
        # question back to the visitor and a request for someone unavailable get none, as today.
        evidence = True

        if result.kind in (RouteKind.PROPOSE, RouteKind.CONFIRM):
            assert result.proposal is not None
            checked = ActionContractValidator(
                self._definition, self._snapshot, definition_version=self._version,
            ).validate(result.proposal, confirmation=result.confirmation_reason)
            if isinstance(checked, Refusal):
                refusal = checked
                next_memory = next_memory.discard_pending()
                stage, evidence = TurnStage.REFUSED, False
                reply = composer.refused(REFUSAL_REPLIES.get(checked.code, "fallback"), scope=SCOPE_NAME)
            else:
                validated = checked
                next_memory = remember_accepted(next_memory, result)
                stage, reply = self._action_reply(composer, result, checked.action)
        elif result.kind == RouteKind.REFUSE:
            stage, evidence = TurnStage.REFUSED, False
            reply = composer.refused(result.response_key or "fallback", scope=SCOPE_NAME,
                                     **dict(result.placeholders))
        elif result.kind == RouteKind.CLARIFY:
            stage, evidence = TurnStage.CLARIFICATION, False
            reply = composer.clarification(result.response_key or "", **self._question_values(next_memory))
        elif result.kind == RouteKind.CANCELLED:
            stage, evidence = TurnStage.CANCELLED, False
            reply = composer.cancelled()
        elif result.kind == RouteKind.ANSWER:
            # The router answers only to say a named person is not available. Unknown and
            # inaccessible people read the same.
            stage, evidence = TurnStage.ANSWER, False
            reply = composer.refused(result.response_key or "fallback", scope=SCOPE_NAME,
                                     **dict(result.placeholders))
        else:
            stage, reply, passages = self._after_fallback(text, message)

        if evidence and not passages and self._knowledge is not None:
            passages = ground(self._knowledge, message).passages

        translated, missing = self._translated(validated)
        person_name = None
        if validated is not None and result.person is not None:
            record = self._snapshot.get(result.person.entity, result.person.id)
            person_name = record.title if record is not None else None
        feature = self._signals.feature(text, self._subject(validated))
        signals = self._signals.signals(text, feature=feature, person_name=person_name)
        next_history = history.add(signals)
        return EngineTurn(
            stage=stage,
            route=result,
            validated=validated,
            refusal=refusal,
            reply=reply,
            legacy_action=translated,
            translation_missing=missing,
            feature=feature,
            profile=self._signals.profile(text),
            signals=signals,
            passages=passages,
            summary=session_summary(next_memory, next_history),
            memory=next_memory,
            history=next_history,
        )

    # --- stages ---

    def _action_reply(
        self, composer: ResponseComposer, result: RouteResult, action: GenericAction,
    ) -> tuple[TurnStage, Reply]:
        values = self._action_values(action)
        if result.kind == RouteKind.CONFIRM:
            return TurnStage.AWAITING_CONFIRMATION, composer.awaiting_confirmation(action, **values)
        stage = TurnStage.WOULD_EXECUTE if result.confirmed else TurnStage.PROPOSED
        reply = composer.proposed(action, **values)
        if result.response_key == "unknown_person":
            # The request went ahead without the person it named; say so first, in platform words.
            missing = composer.refused("unknown_person", scope=SCOPE_NAME, **dict(result.placeholders))
            reply = Reply(f"{missing.speech} {reply.speech}", reply.stage, reply.template_key)
        return stage, reply

    def _after_fallback(
        self, text: NormalizedMessage, message: str,
    ) -> tuple[TurnStage, Reply, tuple[KnowledgePassage, ...]]:
        conversational = detect(text)
        if conversational is not None:
            composer = ResponseComposer(self._definition, visitor_name=conversational.visitor_name)
            if conversational.kind == Conversational.CAPABILITIES:
                offers = offerable(self._definition, self._snapshot, self._policy)
                return TurnStage.ANSWER, composer.capabilities(offers), ()
            return TurnStage.ANSWER, composer.answer(conversational.template_key), ()
        composer = ResponseComposer(self._definition)
        if self._knowledge is not None and _is_question(text):
            grounding = ground(self._knowledge, message)
            reply = composer.knowledge_answer(grounding)
            if grounding.is_grounded and reply.template_key != KNOWLEDGE_UNAVAILABLE_KEY:
                return TurnStage.KNOWLEDGE, reply, grounding.passages
            return TurnStage.UNGROUNDED, reply, ()
        return TurnStage.FALLBACK, composer.answer("fallback"), ()

    # --- values the platform wording needs ---

    def _action_values(self, action: GenericAction) -> dict[str, str]:
        values: dict[str, str] = {}
        if action.view is not None:
            values["view"] = self._view_label(action.view)
        if action.fields:
            # People are named, never spoken as identifiers.
            entity = self._definition.actions[action.action_key].entity
            values["changes"] = describe_changes({
                name: self._display(entity, name, value) for name, value in action.fields.items()
            })
        return values

    def _display(self, entity: str | None, field: str, value: Any) -> Any:
        spec = self._definition.entities[entity].fields.get(field) if entity in self._definition.entities else None
        if spec is None or spec.type != "ref" or not isinstance(value, str):
            return value
        record = self._snapshot.get(spec.target or "", value)
        return record.title if record is not None and record.title else value

    def _subject(self, validated: ValidatedAction | None) -> tuple[str, ...]:
        """The names of what a validated action acts on, for choosing the turn's topic."""
        if validated is None:
            return ()
        action = validated.action
        names: list[str] = []
        if action.view is not None:
            names.append(action.view)
            if (view := self._definition.views.get(action.view)) is not None:
                names.append(view.label)
        key = self._definition.actions[action.action_key].entity
        if key is not None and (entity := self._definition.entities.get(key)) is not None:
            names += [key, entity.label, entity.plural]
        return tuple(names)

    def _question_values(self, memory: ConversationMemory) -> dict[str, str]:
        """Names for the question just asked, drawn from what the router stored with it."""
        pending = memory.pending_clarification
        values = {"label": "record", "record_id": "this record", "view": "", "records": ""}
        if pending is None:
            return values
        if pending.action_key is not None:
            spec = self._definition.actions[pending.action_key]
            if spec.entity is not None:
                values["label"] = self._definition.entities[spec.entity].label.lower()
            if spec.view is not None:
                values["view"] = self._view_label(spec.view)
        if pending.target is not None:
            values["record_id"] = pending.target.id
        names = []
        for ref in pending.candidates:
            record = self._snapshot.get(ref.entity, ref.id)
            names.append(record.title if record is not None and record.title else ref.id)
        values["records"] = ", ".join(names)
        return values

    def _view_label(self, view: str) -> str:
        spec = self._definition.views.get(view)
        return spec.label if spec is not None else view.replace("_", " ").capitalize()

    def _translated(self, validated: ValidatedAction | None) -> tuple[Any | None, str | None]:
        if validated is None or self._translate is None:
            return None, None
        try:
            return self._translate(validated), None
        except LookupError as missing:
            # A missing mapping is recorded, never replaced by a guess.
            return None, str(missing) or type(missing).__name__


def _is_question(text: NormalizedMessage) -> bool:
    words = text.full.split()
    return text.original.rstrip().endswith("?") or bool(words and words[0] in QUESTION_OPENERS)
