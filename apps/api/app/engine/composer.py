"""Deterministic replies for the generic engine (3.2 slice 4b).

The platform owns every sentence, including introductions and clarifications. Product response
bodies and action descriptions are never spoken. Definitions supply validated names; verified
actions and committed results supply facts. The caller must choose the correct lifecycle stage.

Knowledge excerpts use a fixed attribution. Document titles are metadata, never spoken prose.
No model-written sentence is accepted in this slice.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.definitions.contract import ProductDefinition
from app.definitions.copy_rules import name_problems
from app.definitions.safety import check_text
from app.engine.actions import GenericAction
from app.engine.conversation import OfferableActions, capability_sentence, guided_steps
from app.engine.knowledge import Grounding

# Words that assert a change already happened. Checked against model-written speech only.
COMPLETION_CLAIMS = (
    "done", "i've", "i have", "has been", "have been", "is now", "are now", "updated it",
    "created it", "assigned it", "closed it", "changed it", "moved it", "successfully",
    "completed", "all set", "that's set", "i updated", "i created", "i assigned", "i changed",
    "i closed", "i moved", "i've added", "added it",
)

_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


class Stage(StrEnum):
    """What has actually happened, which is what the wording must match."""

    PROPOSED = "proposed"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    EXECUTED = "executed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CLARIFICATION = "clarification"
    ANSWER = "answer"
    REFUSED = "refused"
    # A question nothing installed can answer. Not a refusal: the request was fine, the
    # deployment simply has no source for it.
    UNGROUNDED = "ungrounded"


# Stages where a model may write the sentence itself. **Empty, deliberately.**
#
# Executing one action proves that one action succeeded; it does not make any other sentence true,
# and "I deleted every customer" passes every lexical check ever written. Retrieving a passage
# proves a document exists; it does not make a sentence about that document accurate. Until a
# reply can be bound to its citation and the binding evaluated (3.4 and later), every word the
# assistant says is composed deterministically from platform wording and the committed result.
MODEL_SPEECH_STAGES: frozenset[Stage] = frozenset()

# The parser already caps model speech; the composer does not rely on that being the only path.
MAX_MODEL_SPEECH = 2000

# Which templates each stage may use. **Enforced**, not advisory: a stage can only ever say
# something its own list allows, so a failed action can never reach for success wording.
STAGE_TEMPLATES: Mapping[Stage, frozenset[str]] = {
    Stage.PROPOSED: frozenset({"record_create_proposed", "record_update_proposed", "view_opened",
                               "record_opened", "records_filtered", "records_found",
                               "control_highlighted", "view_switched"}),
    Stage.AWAITING_CONFIRMATION: frozenset({"confirm_action"}),
    Stage.EXECUTED: frozenset({"record_created", "record_updated", "view_opened", "record_opened",
                               "records_filtered", "control_highlighted"}),
    Stage.CANCELLED: frozenset({"action_cancelled"}),
    # A failure explains itself. None of these can assert that anything changed.
    Stage.FAILED: frozenset({"fallback", "next_step", "nothing_changed", "out_of_scope",
                             "destructive_refused", "person_outside_scope", "work_outside_scope",
                             "broad_scope_refused", "member_missing", "unknown_person"}),
    Stage.CLARIFICATION: frozenset({"clarify_create", "clarify_assign", "clarify_owner", "clarify_change",
                                    "clarify_all_items", "clarify_update_target", "clarify_person",
                                    "correction"}),
    Stage.REFUSED: frozenset({"out_of_scope", "destructive_refused", "person_outside_scope",
                              "work_outside_scope", "broad_scope_refused", "unknown_person",
                              "member_missing", "fallback"}),
    Stage.ANSWER: frozenset({"greeting", "greeting_named", "greeting_again", "identity",
                             "capabilities", "fallback",
                             "profile_acknowledged",
                             "guided_path", "next_step", "last_change", "nothing_changed",
                             "people_count", "people_count_here", "anchor_count",
                             "anchor_count_here", "anchor_count_none", "conversation_ended", "thanks",
                             "voice_interruption", "next_step_here", "knowledge_unavailable"}),
    Stage.UNGROUNDED: frozenset({"knowledge_unavailable"}),
}

# Lifecycle assertions are platform-owned. A product may rename itself and its records, but a
# customer-authored response template cannot turn "proposed" or "failed" into "completed".
PLATFORM_LIFECYCLE_TEMPLATES: Mapping[tuple[Stage, str], str] = {
    (Stage.PROPOSED, "record_create_proposed"): "I'll create this record with {changes}.",
    (Stage.PROPOSED, "record_update_proposed"): "I'll update {record_id}: {changes}.",
    (Stage.PROPOSED, "view_opened"): "I'll open {view}.",
    # Owner decision: a correction is acknowledged before the navigation it asks for.
    (Stage.PROPOSED, "view_switched"): "Got it. I'll switch to {view}.",
    (Stage.PROPOSED, "record_opened"): "I'll open {record_id}.",
    (Stage.PROPOSED, "records_filtered"): "I'll filter the available records.",
    (Stage.PROPOSED, "records_found"): "I found {count} {label} for {person}: {records}.",
    (Stage.PROPOSED, "control_highlighted"): "I'll open {view} and highlight {control}.",
    (Stage.AWAITING_CONFIRMATION, "confirm_action"): "Should I apply this change: {changes}?",
    (Stage.EXECUTED, "record_created"): "Created {record_id}: {changes}.",
    (Stage.EXECUTED, "record_updated"): "Updated {record_id}: {changes}.",
    (Stage.EXECUTED, "view_opened"): "Opened {view}.",
    (Stage.EXECUTED, "record_opened"): "Opened {record_id}.",
    (Stage.EXECUTED, "records_filtered"): "Filtered the available records.",
    (Stage.EXECUTED, "control_highlighted"): "Opened {view} and highlighted {control}.",
    (Stage.CANCELLED, "action_cancelled"): "Okay, I won't make that change.",
}

LIFECYCLE_STAGES = frozenset({
    Stage.PROPOSED, Stage.AWAITING_CONFIRMATION, Stage.EXECUTED, Stage.FAILED, Stage.CANCELLED,
})
PLATFORM_FAILURE = "I couldn't complete that request."
# A control the definition does not name is described, never spoken as its identifier.
UNNAMED_CONTROL = "the requested control"
KNOWLEDGE_UNAVAILABLE_WORDINGS = (
    "Sorry, I can't answer that. I only know about {product}, and I'd rather not guess.",
    "That's not something I can tell you. I can only answer from approved {product} "
    "information, and it doesn't cover that.",
    "I'm not able to answer that one. Ask me anything about {product} and I'll do my best.",
    "I don't have an answer for that, and I won't make one up. I can help with anything in "
    "{product}.",
)
PLATFORM_KNOWLEDGE_UNAVAILABLE = KNOWLEDGE_UNAVAILABLE_WORDINGS[0]
# Unknown and inaccessible people read identically, so a refusal never reveals that someone
# exists outside the caller's scope.
PLATFORM_PERSON_NOT_FOUND = "I can't find {person} in {scope}."
PLATFORM_NOTHING_OFFERED = "There's nothing I can do for you in {product} right now."

# Every conversational sentence that asserts something. `{label}` is the product's own name for a
# kind of record (an entity label or plural, chosen by the caller for the count); every other
# value is supplied by the platform from what it actually found or did.
PLATFORM_CONVERSATION_TEMPLATES: Mapping[tuple[Stage, str], str] = {
    # Owner decision: names are spoken plainly ("I'm <assistant>"), never in quotation marks.
    (Stage.ANSWER, "greeting"): "Hi, I'm {assistant}, your guide to {product}. What would you like to explore?",
    (Stage.ANSWER, "greeting_named"): "Nice to meet you, {visitor}. What would you like to explore in {product}?",
    (Stage.ANSWER, "greeting_again"): "Hi {visitor}, good to see you again. What would you like to explore next in {product}?",
    (Stage.ANSWER, "profile_acknowledged"): "Thanks, that helps. What would you like to explore first in {product}?",
    (Stage.ANSWER, "identity"): "I'm {assistant}, your guide to {product}. Ask me what I can do, or tell me what you'd like to see.",
    (Stage.CLARIFICATION, "clarify_create"): "Which type of record would you like to create?",
    (Stage.CLARIFICATION, "clarify_all_items"): "Which records do you mean?",
    # Answers: what can be done, what exists, what happened, what is known.
    (Stage.ANSWER, "capabilities"): "Here's what I can do in {product}: {capabilities}.",
    # Owner decision: the guided path is a conversational route, drawn from the caller's offers.
    (Stage.ANSWER, "guided_path"): "Here's a good way to explore {product}: {capabilities}.",
    # A request nobody could place is the moment somebody most needs to know what is possible,
    # so the answer offers the way out rather than ending the conversation.
    (Stage.ANSWER, "fallback"): (
        "I'm not sure how to help with that in {product}. I can {capabilities}. "
        "What would you like to do?"
    ),
    (Stage.ANSWER, "next_step"): "Ask what I can do in {product} to see where to go next.",
    (Stage.ANSWER, "next_step_here"): "From {view}, you could {capabilities}.",
    (Stage.ANSWER, "last_change"): "The most recent change: {changes}.",
    (Stage.ANSWER, "nothing_changed"): "Nothing has changed in this conversation yet.",
    (Stage.ANSWER, "people_count"): "{scope} has {count} {label}.",
    (Stage.ANSWER, "people_count_here"): "You have {count} {label}.",
    (Stage.ANSWER, "anchor_count"): "{scope} has {count} visible {label}: {records}.",
    # When there is no narrower place than the product itself, naming it twice reads as though
    # somewhere else were meant. The plain form is what a person would say.
    (Stage.ANSWER, "anchor_count_here"): "You have {count} {label}: {records}.",
    (Stage.ANSWER, "anchor_count_none"): "You don't have any {label} yet.",
    (Stage.ANSWER, "conversation_ended"): "Okay, we can stop here. Come back whenever you like.",
    (Stage.ANSWER, "thanks"): "Happy to help. Anything else you would like to see in {product}?",
    # Voice is a platform feature, so its behaviour is the platform's to describe.
    (Stage.ANSWER, "voice_interruption"): (
        "When you start speaking, I stop the current response, listen for the completed thought, "
        "then run the new request through the same scoped action checks."
    ),
    (Stage.ANSWER, "knowledge_unavailable"): PLATFORM_KNOWLEDGE_UNAVAILABLE,
    (Stage.UNGROUNDED, "knowledge_unavailable"): PLATFORM_KNOWLEDGE_UNAVAILABLE,
    # Refusals.
    (Stage.REFUSED, "out_of_scope"): "I can only help with {product} here, so I can't do that.",
    (Stage.REFUSED, "destructive_refused"): "I can't delete or erase anything here.",
    (Stage.REFUSED, "broad_scope_refused"): (
        "I can only work within {scope}. Change the scope first, then ask again."
    ),
    (Stage.REFUSED, "unknown_person"): PLATFORM_PERSON_NOT_FOUND,
    (Stage.REFUSED, "person_outside_scope"): PLATFORM_PERSON_NOT_FOUND,
    (Stage.REFUSED, "member_missing"): PLATFORM_PERSON_NOT_FOUND,
    (Stage.REFUSED, "work_outside_scope"): "I can't find that in {scope}.",
    (Stage.REFUSED, "fallback"): "I can't help with that here.",
    # Slot questions: which person or record an action needs. The answer feeds an action the
    # platform may still refuse, so the question promises nothing about what happens next.
    (Stage.CLARIFICATION, "clarify_person"): "Which person do you mean: {records}?",
    (Stage.CLARIFICATION, "clarify_assign"): "Who should {record_id} be assigned to?",
    (Stage.CLARIFICATION, "clarify_owner"): "Who should own the new {label}?",
    # Owner decision: the question says how to answer it.
    (Stage.CLARIFICATION, "clarify_update_target"): "Which {label} do you mean? Open it first, or tell me which one.",
    (Stage.CLARIFICATION, "clarify_change"): "What should I change about {record_id}?",
    (Stage.CLARIFICATION, "correction"): "Got it, {view} instead.",
}


# Replies that say "not here" come in several wordings, and a conversation moves through them
# turn by turn. The same refusal twice in a row reads like a machine that has stopped listening;
# every wording still says plainly what the assistant cannot do and what it can.
VARIED_TEMPLATES: Mapping[tuple[Stage, str], tuple[str, ...]] = {
    (Stage.ANSWER, "fallback"): (
        "Sorry, I can't help with that in {product}. I can {capabilities}. What would you like to do?",
        "That's outside what I can do here, I'm afraid. In {product} I can {capabilities}. "
        "What would you like to try?",
        "I didn't quite follow that one. I can {capabilities}. Where would you like to start?",
        "I'm not able to help with that in {product}, but I can {capabilities}. "
        "What should we do next?",
    ),
    (Stage.REFUSED, "out_of_scope"): (
        "Sorry, I can't do that. I can only help with {product} here.",
        "That's outside {product}, so it isn't something I can do for you.",
        "I'm afraid I can't help with that. My work is limited to {product}.",
        "I can't do that from here. I only have access to {product}.",
    ),
    (Stage.REFUSED, "fallback"): (
        "Sorry, I can't help with that here.",
        "That isn't something I can do here.",
        "I'm afraid I can't help with that one.",
    ),
    (Stage.REFUSED, "destructive_refused"): (
        "Sorry, I can't delete or erase anything here.",
        "Deleting isn't something I'm able to do, so nothing has been removed.",
        "I can't delete or erase anything, so everything stays as it is.",
    ),
    (Stage.ANSWER, "knowledge_unavailable"): KNOWLEDGE_UNAVAILABLE_WORDINGS,
    (Stage.UNGROUNDED, "knowledge_unavailable"): KNOWLEDGE_UNAVAILABLE_WORDINGS,
}


class TemplateNotAllowed(ValueError):
    """A stage was asked to speak with wording that does not belong to it."""


class UnsafeProductCopy(ValueError):
    """Product copy that asserts state reached the composer without passing validation."""

# A completed mutation and a proposed one use different templates for the same action.
EXECUTED_BY_CAPABILITY = {
    "CREATE_RECORD": "record_created",
    "UPDATE_RECORD": "record_updated",
    "NAVIGATE_VIEW": "view_opened",
    "OPEN_RECORD": "record_opened",
    "FILTER_RECORDS": "records_filtered",
    "HIGHLIGHT_CONTROL": "control_highlighted",
}
PROPOSED_BY_CAPABILITY = {
    "CREATE_RECORD": "record_create_proposed",
    "UPDATE_RECORD": "record_update_proposed",
    "NAVIGATE_VIEW": "view_opened",
    "OPEN_RECORD": "record_opened",
    "FILTER_RECORDS": "records_filtered",
    "HIGHLIGHT_CONTROL": "control_highlighted",
}


class MissingTemplate(LookupError):
    """The definition declares no wording for something the platform needs to say."""


@dataclass(frozen=True)
class Reply:
    """What the assistant says, and the evidence for why it was allowed to say it."""

    speech: str
    stage: Stage
    template_key: str | None
    from_model: bool = False
    replaced_model_speech: bool = False
    sources: tuple[str, ...] = ()
    # Compatibility field: always false while all response sentences are platform-owned.
    product_copy: bool = False
    source_titles: tuple[str, ...] = ()


class ResponseComposer:
    """Turns verified state into platform wording; products contribute names only."""

    def __init__(self, definition: ProductDefinition, *, visitor_name: str | None = None,
                 turn: int = 1) -> None:
        for name in (definition.identity.product_name, definition.identity.assistant_name):
            if name_problems(name):
                raise UnsafeProductCopy("identity must contain plain names")
        self._definition = definition
        self._visitor = visitor_name
        # Which wording a varied reply uses: the first on a conversation's first turn, the next
        # on the next, so two refusals in a row never read the same.
        self._turn = max(turn, 1)

    # --- the lifecycle ---

    def proposed(self, action: GenericAction, **values: str) -> Reply:
        key = PROPOSED_BY_CAPABILITY[str(action.capability)]
        return self._render_lifecycle(Stage.PROPOSED, key, self._action_values(action, values))

    def records_found(self, action: GenericAction, *, count: int, label: str, person: str,
                      records: str, **values: str) -> Reply:
        """A filter proposal that says what it found; every value comes from the snapshot."""
        described = self._action_values(action, {**values, "count": str(count), "label": label,
                                                  "person": person, "records": records})
        return self._render_lifecycle(Stage.PROPOSED, "records_found", described)

    def awaiting_confirmation(self, action: GenericAction, **values: str) -> Reply:
        described = self._action_values(action, values)
        template = (
            "Should I update {record_id}: {changes}?"
            if action.target is not None
            else "Should I create this record with {changes}?"
        )
        return self._render_platform(Stage.AWAITING_CONFIRMATION, "confirm_action", template,
                                     described)

    def executed(self, action: GenericAction, **values: str) -> Reply:
        key = EXECUTED_BY_CAPABILITY[str(action.capability)]
        return self._render_lifecycle(Stage.EXECUTED, key, self._action_values(action, values))

    def failed(self, action: GenericAction, reason_key: str = "fallback", **values: str) -> Reply:
        """A rejected change is never described as done, and never invents a cause.

        `reason_key` is checked against this stage's allowlist, so a failure cannot be worded with
        a template that asserts success.
        """
        return self._render_lifecycle(Stage.FAILED, reason_key, self._action_values(action, values))

    def cancelled(self, **values: str) -> Reply:
        return self._render_lifecycle(Stage.CANCELLED, "action_cancelled", values)

    def clarification(self, template_key: str, **values: str) -> Reply:
        return self._render(Stage.CLARIFICATION, template_key, values)

    def field_question(self, template: str, **values: str) -> Reply:
        """A question for one missing required field (5c plan, section 7.1).

        The platform owns the question's structure (`field_completion`); the definition supplies
        only the entity and field labels and the allowed values it names.
        """
        return self._render_platform(Stage.CLARIFICATION, "missing_field", template, values)

    def refused(self, template_key: str, **values: str) -> Reply:
        return self._render(Stage.REFUSED, template_key, values)

    def answer(self, template_key: str, **values: str) -> Reply:
        return self._render(Stage.ANSWER, template_key, values)

    def capabilities(self, offers: OfferableActions) -> Reply:
        """What this caller can actually do, from the filtered offers; never a product's claim."""
        return self._offer_reply("capabilities", offers)

    def unplaceable(self, offers: OfferableActions) -> Reply:
        """A request this product could not place, answered with what it can do instead.

        A reply that only says it did not understand leaves somebody with nowhere to go. The
        options are the caller's own offers, so this can never promise something they cannot do.
        """
        return self._offer_reply("fallback", offers)

    def guided_path(self, offers: OfferableActions) -> Reply:
        """Where to start, drawn from the same filtered offers as the capability reply."""
        if offers.is_empty:
            return self._render_platform(Stage.ANSWER, "guided_path", PLATFORM_NOTHING_OFFERED, {})
        return self._render(Stage.ANSWER, "guided_path", {"capabilities": guided_steps(offers, self._definition)})

    def view_switched(self, action: GenericAction, **values: str) -> Reply:
        """A navigation the visitor asked for by correcting themselves."""
        return self._render_lifecycle(Stage.PROPOSED, "view_switched", self._action_values(action, values))

    def next_step(self, offers: OfferableActions, view: str) -> Reply:
        """What the caller could do from the view they have open, from the filtered offers only."""
        if offers.is_empty:
            return self._render(Stage.ANSWER, "next_step", {})
        return self._render(Stage.ANSWER, "next_step_here", {
            "view": view, "capabilities": capability_sentence(offers, self._definition, limit=3),
        })

    def _offer_reply(self, key: str, offers: OfferableActions) -> Reply:
        if offers.is_empty:
            return self._render_platform(Stage.ANSWER, key, PLATFORM_NOTHING_OFFERED, {})
        return self._render(Stage.ANSWER, key, {"capabilities": capability_sentence(offers, self._definition)})

    def knowledge_answer(self, grounding: Grounding) -> Reply:
        """Answer with an approved passage, or say plainly that there is nothing to answer from.

        Having retrieved something is not the same as being grounded in it. A model sentence that
        merely *accompanies* a passage can say anything at all, so this slice speaks the passage
        itself. Model synthesis needs citation binding and a grounding evaluation, which are not
        in this slice.
        """
        if not grounding.is_grounded:
            return self._render_platform(
                Stage.UNGROUNDED, "knowledge_unavailable", PLATFORM_KNOWLEDGE_UNAVAILABLE, {}
            )
        passage = grounding.passages[0]
        snippet = passage.snippet
        title = passage.title or "Product documentation"
        if (not _is_plain(snippet) or len(snippet) > 1800 or '"' in snippet
                or not _is_plain(title) or len(title) > 160):
            return self._render_platform(
                Stage.UNGROUNDED, "knowledge_unavailable", PLATFORM_KNOWLEDGE_UNAVAILABLE, {}
            )
        # Always attributed, so a document saying "Done, I have updated it" is never heard as the
        # assistant claiming something it did. The attribution is fixed wording: a title is written
        # by whoever approved the document and is never spoken.
        speech = f"From the product documentation: {snippet}"
        return Reply(speech, Stage.ANSWER, None, sources=(passage.source,), source_titles=(title,))

    # --- model-written speech ---

    def from_model(self, speech: str, stage: Stage, fallback_key: str, **values: str) -> Reply:
        """Consider a model-written sentence, and in this slice always replace it.

        The rule is structural, not lexical, because no lexical rule survives contact with a model
        that writes English. A sentence is only trustworthy if it can be tied to what actually
        happened, and nothing in this slice can make that tie: an execution result proves one
        operation, and a retrieved passage proves one document. So every reply is composed from
        platform wording, and this method records that the model's sentence was dropped.
        """
        if stage in MODEL_SPEECH_STAGES and _is_plain(speech) and len(speech) <= MAX_MODEL_SPEECH \
                and not (stage is not Stage.EXECUTED and _claims_completion(speech)):
            return Reply(speech.strip(), stage, None, from_model=True)
        replaced = (
            self._render_lifecycle(stage, fallback_key, values)
            if stage in LIFECYCLE_STAGES
            else self._render(stage, fallback_key, values)
        )
        return Reply(replaced.speech, stage, replaced.template_key, from_model=False,
                     replaced_model_speech=True, product_copy=replaced.product_copy)

    # --- rendering ---

    def _render(self, stage: Stage, key: str, values: Mapping[str, str]) -> Reply:
        allowed = STAGE_TEMPLATES.get(stage, frozenset())
        if key not in allowed:
            raise TemplateNotAllowed(f"{stage} may not be worded with {key}")
        template = PLATFORM_CONVERSATION_TEMPLATES.get((stage, key))
        if template is None:
            raise TemplateNotAllowed(f"no platform wording for {stage}:{key}")
        return self._render_platform(stage, key, template, values)

    def _render_lifecycle(self, stage: Stage, key: str, values: Mapping[str, str]) -> Reply:
        allowed = STAGE_TEMPLATES.get(stage, frozenset())
        if key not in allowed:
            raise TemplateNotAllowed(f"{stage} may not be worded with {key}")
        if stage is Stage.FAILED:
            return Reply(PLATFORM_FAILURE, stage, key)
        template = PLATFORM_LIFECYCLE_TEMPLATES.get((stage, key))
        if template is None:
            raise TemplateNotAllowed(f"no platform lifecycle wording for {stage}:{key}")
        # Every path (including a replaced model sentence, which has no action) can word a highlight.
        return self._render_platform(stage, key, template, {"control": UNNAMED_CONTROL, **values})

    def _render_platform(
        self, stage: Stage, key: str, template: str, values: Mapping[str, str],
    ) -> Reply:
        wordings = VARIED_TEMPLATES.get((stage, key))
        if wordings:
            template = wordings[(self._turn - 1) % len(wordings)]
        return Reply(self._fill(template, values), stage, key)

    def _fill(self, template: str, values: Mapping[str, str]) -> str:
        supplied = {
            **{name: str(value) for name, value in values.items() if value is not None},
            "product": self._definition.identity.product_name,
            "assistant": self._definition.identity.assistant_name,
            **({"visitor": self._visitor} if self._visitor else {}),
        }
        missing = set(_PLACEHOLDER.findall(template)) - set(supplied)
        if missing:
            raise MissingTemplate(f"no value for {', '.join(sorted(missing))}")
        return _PLACEHOLDER.sub(lambda match: supplied[match.group(1)], template).strip()

    def _action_values(self, action: GenericAction, values: Mapping[str, str]) -> dict[str, str]:
        described = dict(values)
        described.setdefault("record_id", action.target.id if action.target else "")
        described.setdefault("view", action.view or "")
        if action.control is not None:
            # A control is named by its definition label, never by its identifier.
            view = self._definition.views.get(action.view or "")
            control = view.controls.get(action.control) if view is not None else None
            described.setdefault("control", control.label if control is not None else UNNAMED_CONTROL)
        if action.fields:
            described.setdefault("changes", describe_changes(action.fields))
        return {name: value for name, value in described.items() if value != "" or name in values}


def describe_changes(fields: Mapping[str, object]) -> str:
    """"status to Closed, owner to Ana Lopez" — the exact change, in the visitor's terms."""
    return ", ".join(f"{name} to {value}" for name, value in sorted(fields.items()))


def _claims_completion(speech: str) -> bool:
    lowered = f" {speech.lower()} "
    return any(f"{claim}" in lowered for claim in COMPLETION_CLAIMS)


def _is_plain(speech: str) -> bool:
    if not speech or not speech.strip():
        return False
    try:
        check_text(speech)
    except ValueError:
        return False
    return True


# --- Execution receipts (5b plan, section 8) ---
#
# What the assistant says after a keyed write, composed from the committed outcome only. Receipt text is
# never stored: a first execution names the values the key bound, a replayed success says only that
# the change was already applied, and every failure is worded by its stored code alone, so replaying
# a failure repeats the same sentence.

RECEIPT_REPLAYED = "This change was already applied."
RECEIPT_REPLAYED_UNAVAILABLE = "This change was already applied, and that record is no longer available here."
RECEIPT_FAILURES: Mapping[str, str] = {
    "superseded": "A newer request replaced this change, so it wasn't applied.",
    "user_cancelled": "This change was cancelled before it was applied.",
    "expired": "This change expired before it was applied.",
    "record_conflict": "The record changed or is no longer available, so this change wasn't applied.",
    "record_not_found": "The record changed or is no longer available, so this change wasn't applied.",
    "scope_mismatch": "This change is not available in this workspace, so it wasn't applied.",
    "scope_unavailable": "This change is not available in this workspace, so it wasn't applied.",
    "invalid_change": "This change isn't valid for that record, so it wasn't applied.",
}
RECEIPT_REJECTED = RECEIPT_FAILURES["invalid_change"]


def receipt_speech(
    code: str, *, executed: bool, replay: bool, created: bool = False, record_id: str | None = None,
    changes: Mapping[str, object] | None = None,
) -> str:
    """The platform sentence for one keyed-write outcome."""
    if executed:
        if code == "execution_result_unavailable":
            return RECEIPT_REPLAYED_UNAVAILABLE
        if replay:
            return RECEIPT_REPLAYED
        template = PLATFORM_LIFECYCLE_TEMPLATES[
            (Stage.EXECUTED, "record_created" if created else "record_updated")
        ]
        return template.format(record_id=record_id or "the record", changes=describe_changes(changes or {}))
    return RECEIPT_FAILURES.get(code, RECEIPT_REJECTED)
