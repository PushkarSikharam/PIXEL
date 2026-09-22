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
from dataclasses import dataclass, replace
from enum import StrEnum
import re
from typing import Any

from app.definitions.contract import EntitySpec, ProductDefinition
from app.definitions.vocabulary import MUTATING_CAPABILITIES, Capability
from app.engine.actions import GenericAction
from app.engine.composer import Reply, ResponseComposer, describe_changes
from app.engine.field_completion import first_missing, question_values, read_answer
from app.engine.conversation import (
    KNOWLEDGE_UNAVAILABLE_KEY,
    CapabilityPolicy,
    Conversational,
    detect,
    offerable,
)
from app.engine.knowledge import KnowledgeLookup, KnowledgePassage, ground
from app.engine.memory import ConversationMemory, PendingClarification
from app.engine.normalizer import NormalizedMessage
from app.engine.router import IntentRouter, TurnContext, remember_accepted
from app.engine.routing import RouteKind, RouteResult, RouteStage
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

# A visitor describing themselves ("I'm a ...", "we're an ..."), as opposed to asking for something.
_SELF_DESCRIPTION = re.compile(r"^\s*(i am|i'm|im|we are|we're)\s+(a|an|the)\s+\w+", re.IGNORECASE)
# Words that turn a self-description into a request ("I'm a manager, show me what's open").
_REQUEST_MARKER = re.compile(
    r"\b(show|open|go to|take me|create|make|add|assign|set|change|update|move|filter|find|list|"
    r"explain|tell me|help me|can you|could you|would you|please|want to|would like|i'd like|let me|how|"
    r"what|why|where|which|who)\b|\?",
    re.IGNORECASE,
)
# The response key of a question for one missing required field (5c plan, section 7.1).
MISSING_FIELD_KEY = "missing_field"
# Words that set an unfinished create aside instead of answering its question.
_SET_ASIDE = re.compile(r"^\s*(cancel|stop|never ?mind|forget it|leave it|skip it|no thanks)\b", re.IGNORECASE)
# While a free-text field is being asked for, only these requests replace the unfinished create;
# anything else is the answer (a title may well mention a product area).
_REPLACING = frozenset({Capability.NAVIGATE_VIEW, Capability.CREATE_RECORD, Capability.UPDATE_RECORD})



def _describes_visitor(message: str) -> bool:
    """Whether the whole message is the visitor describing themselves, with no request in it."""
    return bool(_SELF_DESCRIPTION.match(message)) and not _REQUEST_MARKER.search(message)


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

    @property
    def definition(self) -> ProductDefinition:
        return self._definition

    @property
    def snapshot(self) -> TurnSnapshot:
        """The one snapshot this engine validates against; model evidence must use the same one."""
        return self._snapshot

    def turn(
        self,
        message: str,
        memory: ConversationMemory,
        history: SignalHistory,
        context: TurnContext,
    ) -> EngineTurn:
        # A router per turn: it keeps working state while routing, and turns never share it.
        router = IntentRouter(self._definition, self._snapshot)
        text = router.normalizer.normalize(message)
        composer = ResponseComposer(self._definition)
        validated: ValidatedAction | None = None
        refusal: Refusal | None = None
        passages: tuple[KnowledgePassage, ...] = ()
        # Whether documents are retrieved as evidence for this reply (never spoken). A refusal, a
        # question back to the visitor and a request for someone unavailable get none, as today.
        evidence = True
        # Whether this turn began a create that now waits for a missing field. The person the
        # visitor named for it is already resolved, so it is recorded like any accepted request.
        drafted = False

        continued = self._continue_create(message, memory, context, composer)
        if continued is not None:
            result, next_memory, stage, reply, validated, refusal = continued
            evidence = False
        else:
            # Only this engine reads a missing-value question; the router never sees one.
            pending = memory.pending_clarification
            if pending is not None and pending.field is not None:
                memory = replace(memory, pending_clarification=None)
            routed = router.route(message, memory, context)
            result, next_memory = routed.result, routed.memory

        if continued is not None:
            pass  # the unfinished create decided this turn
        elif (result.kind == RouteKind.PROPOSE and result.proposal is not None
                and result.proposal.capability not in MUTATING_CAPABILITIES and _describes_visitor(message)):
            # "I'm a manager moving from another tool" may name a word a view matches, but it
            # asks for nothing; opening that view would answer a request nobody made. A mutation, a
            # refusal, a question back or a confirmation is never replaced this way.
            next_memory = memory
            stage, reply, passages, evidence = self._after_fallback(text, message, context)
        elif result.kind in (RouteKind.PROPOSE, RouteKind.CONFIRM):
            assert result.proposal is not None
            checked = ActionContractValidator(
                self._definition, self._snapshot, definition_version=self._version,
            ).validate(result.proposal, confirmation=result.confirmation_reason)
            if (isinstance(checked, Refusal) and checked.code == "missing_required_field"
                    and result.proposal.capability == Capability.CREATE_RECORD):
                # A create missing a required field asks for it; it is never refused or guessed.
                _, next_memory, stage, reply, validated, refusal = self._propose_create(
                    composer, next_memory, result.proposal, context,
                )
                drafted, evidence = stage == TurnStage.CLARIFICATION, False
            elif isinstance(checked, Refusal):
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
            stage, reply, passages, evidence = self._after_fallback(text, message, context)

        if evidence and not passages and self._knowledge is not None:
            passages = ground(self._knowledge, message).passages

        translated, missing = self._translated(validated)
        person_name = None
        if (validated is not None or drafted) and result.person is not None:
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

    # --- required-field completion (5c plan, section 7.1) ---

    def _continue_create(self, message: str, memory: ConversationMemory, context: TurnContext,
                         composer: ResponseComposer):
        """Read this turn as the answer to a missing-value question, if one is waiting.

        Returns None when there is no such question, it has expired, or the message is a new
        request (which discards the unfinished create and is routed normally).
        """
        advanced = memory.next_turn(context.turn)
        pending = advanced.pending_clarification
        if pending is None or pending.field is None or pending.action_key is None:
            return None
        cleared = replace(advanced, pending_clarification=None)
        entity = self._entity_of(pending.action_key)
        spec = entity.fields[pending.field]
        if _SET_ASIDE.match(message):
            return self._set_aside(composer, cleared)
        value = read_answer(spec, message, self._snapshot)
        if value is None or spec.type == "text":
            probe = IntentRouter(self._definition, self._snapshot).route(message, cleared, context).result
            if _replaces_create(probe, free_text=spec.type == "text"):
                return None
        if value is None:
            repeated = pending.repeated(context.turn)
            if repeated is None:
                return self._set_aside(composer, cleared)
            return self._ask(composer, replace(advanced, pending_clarification=repeated), repeated, entity)
        fields = {**dict(pending.fields or {}), pending.field: value}
        proposal = GenericAction.for_definition(self._definition, pending.action_key, fields=fields)
        return self._propose_create(composer, cleared, proposal, context)

    def _propose_create(self, composer: ResponseComposer, memory: ConversationMemory,
                        proposal: GenericAction, context: TurnContext):
        """Validate a (partly answered) create against this turn's snapshot; ask for the next
        missing field, refuse, or propose it."""
        result = RouteResult(RouteKind.PROPOSE, RouteStage.PENDING_CLARIFICATION, None, proposal=proposal)
        checked = ActionContractValidator(
            self._definition, self._snapshot, definition_version=self._version,
        ).validate(proposal)
        if isinstance(checked, Refusal):
            entity = self._entity_of(proposal.action_key)
            missing = first_missing(entity, dict(proposal.fields or {}))
            if checked.code == "missing_required_field" and missing is not None:
                pending = PendingClarification(
                    MISSING_FIELD_KEY, proposal.action_key, "value",
                    fields=dict(proposal.fields or {}), turn=context.turn, field=missing,
                )
                asked = replace(memory, pending_clarification=pending, pending_confirmation=None)
                return self._ask(composer, asked, pending, entity)
            reply = composer.refused(REFUSAL_REPLIES.get(checked.code, "fallback"), scope=SCOPE_NAME)
            return result, memory.discard_pending(), TurnStage.REFUSED, reply, None, checked
        stage, reply = self._action_reply(composer, result, checked.action)
        return result, remember_accepted(memory, result), stage, reply, checked, None

    def _ask(self, composer: ResponseComposer, memory: ConversationMemory,
             pending: PendingClarification, entity: EntitySpec):
        assert pending.field is not None
        template, values = question_values(entity, pending.field, self._snapshot)
        result = RouteResult(RouteKind.CLARIFY, RouteStage.PENDING_CLARIFICATION, MISSING_FIELD_KEY)
        return result, memory, TurnStage.CLARIFICATION, composer.field_question(template, **values), None, None

    @staticmethod
    def _set_aside(composer: ResponseComposer, memory: ConversationMemory):
        result = RouteResult(RouteKind.CANCELLED, RouteStage.PENDING_CLARIFICATION, None)
        return result, memory, TurnStage.CANCELLED, composer.cancelled(), None, None

    def _entity_of(self, action_key: str) -> EntitySpec:
        spec = self._definition.actions[action_key]
        assert spec.entity is not None
        return self._definition.entities[spec.entity]

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
        self, text: NormalizedMessage, message: str, context: TurnContext,
    ) -> tuple[TurnStage, Reply, tuple[KnowledgePassage, ...], bool]:
        """The platform stages after routing fell back; the last value says whether evidence is
        retrieved for the reply."""
        conversational = detect(text)
        if conversational is not None:
            composer = ResponseComposer(self._definition, visitor_name=conversational.visitor_name)
            if conversational.kind == Conversational.CAPABILITIES:
                offers = offerable(self._definition, self._snapshot, self._policy)
                return TurnStage.ANSWER, composer.capabilities(offers), (), True
            if conversational.kind == Conversational.LAST_CHANGE:
                return TurnStage.ANSWER, self._last_change(composer, context.last_change), (), False
            return TurnStage.ANSWER, composer.answer(conversational.template_key), (), True
        composer = ResponseComposer(self._definition)
        if _describes_visitor(message):
            # "I'm a manager moving from another tool": context, recorded as signals. It is
            # acknowledged, and never answered as a failed request (the v2 prospect-signal decision).
            return TurnStage.ANSWER, composer.answer("profile_acknowledged"), (), True
        if self._knowledge is not None and _is_question(text):
            grounding = ground(self._knowledge, message)
            reply = composer.knowledge_answer(grounding)
            if grounding.is_grounded and reply.template_key != KNOWLEDGE_UNAVAILABLE_KEY:
                return TurnStage.KNOWLEDGE, reply, grounding.passages, True
            return TurnStage.UNGROUNDED, reply, (), True
        return TurnStage.FALLBACK, composer.answer("fallback"), (), True

    @staticmethod
    def _last_change(composer: ResponseComposer, change: Any) -> Reply:
        """Only an executed ledger entry is a change; its values are never stored, so none are said."""
        if change is None:
            return composer.answer("nothing_changed")
        verb = "created" if getattr(change, "capability", "") == "CREATE_RECORD" else "updated"
        record = getattr(change, "record_id", None) or "a record"
        return composer.answer("last_change", changes=f"{verb} {record}")

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


def _replaces_create(probe: RouteResult, *, free_text: bool) -> bool:
    """Whether a message, routed on its own, is a new request rather than the awaited answer."""
    if probe.kind == RouteKind.REFUSE:
        return True
    if probe.kind in (RouteKind.PROPOSE, RouteKind.CONFIRM):
        assert probe.proposal is not None
        return not free_text or probe.proposal.capability in _REPLACING
    return probe.kind == RouteKind.CLARIFY and not free_text


def _is_question(text: NormalizedMessage) -> bool:
    words = text.full.split()
    return text.original.rstrip().endswith("?") or bool(words and words[0] in QUESTION_OPENERS)
