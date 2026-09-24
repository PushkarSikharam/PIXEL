"""Platform conversation detection and descriptions of offerable operations.

Detection is generic. A capability's verb, target, and allowed fields come from its contract;
product-authored action descriptions are not evidence of what the assistant can execute.
Adapter support, caller permissions, and visible records restrict the offered actions.
"""
from __future__ import annotations

import re

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from app.definitions.contract import ProductDefinition
from app.definitions.copy_rules import name_problems
from app.definitions.vocabulary import Capability
from app.engine.normalizer import NormalizedMessage, contains_term
from app.engine.snapshot import TurnSnapshot

# Whole-message phrases the platform answers itself. Deliberately narrow: anything longer or more
# specific belongs to the product's own intents, which run first.
GREETINGS = frozenset({
    "hi", "hello", "hey", "hey there", "good morning", "good afternoon", "good evening",
    "hi there", "hello there", "yo",
})
IDENTITY_CUES = (
    "who are you", "what are you", "what is your name", "whats your name", "who am i talking to",
    "are you a bot", "are you human", "are you a person", "who is this",
)
CAPABILITY_CUES = (
    "what can you do", "what do you do", "how can you help", "what can i ask",
    "what are you able to", "what can you show me", "help me", "what can this do",
    "are you capable", "capable of doing",
)
# Questions about what the assistant changed. Answered only from the ledger's record of executed
# changes, passed in by the caller; never from a document and never from memory (5b plan, 8.4).
LAST_CHANGE_CUES = (
    "what changed", "what did you do", "what did you change", "what have you changed",
    "what just changed", "what did you just do", "what did we change", "what did we just change",
    "what have we changed", "what was changed", "what did you update", "what just happened",
)
# "I'm Priya" and similar. Matched against normalized text, which has had apostrophes removed,
# so the cue is "im " rather than "i'm ". Cues that ordinary sentences start with ("call me back
# later", "this is urgent") are deliberately absent: they produced names like "Back Later".
INTRODUCTION_CUES = ("i am ", "im ", "my name is ", "call me ")
# Cues that can only introduce a name, so the name may be typed in lowercase.
EXPLICIT_NAME_CUES = ("my name is ", "call me ")
# What may follow a name without being part of it ("I'm Sam from Acme").
_AFTER_NAME = frozenset({"from", "at", "with", "and", "here"})

# Words that follow an introduction cue without being a name.
NOT_NAMES = frozenset({
    "looking", "here", "trying", "just", "not", "sure", "interested", "new", "back", "done",
    "ready", "good", "fine", "ok", "okay", "working", "wondering", "curious", "the", "a", "an",
    "confused", "lost", "stuck", "sorry", "glad", "happy", "excited", "busy", "tired", "bored",
    "going", "using", "evaluating", "exploring", "checking", "testing", "also", "still", "really",
    "very", "so", "on", "in", "at", "from", "with", "your", "you", "this", "that", "it",
})


# Withdrawing whatever was just asked about. Checked against the corrected text too, so
# "actually, never mind" is a withdrawal and never an answer.
SET_ASIDE = re.compile(
    r"^\s*(cancel|stop|never ?mind|forget it|forget that|leave it|skip it|no thanks|not now|"
    r"dont bother|do not bother)\b", re.IGNORECASE)


def is_set_aside(text: NormalizedMessage) -> bool:
    return bool(SET_ASIDE.match(text.original) or SET_ASIDE.match(text.focused))


# Ways a visitor ends or acknowledges a turn. Answered warmly, never with the fallback.
THANKS_CUES = ("thanks", "thank you", "thankyou", "thx", "ty", "cheers", "appreciate it",
               "that helps", "perfect", "great thanks")
CLOSING_CUES = ("bye", "goodbye", "see you", "that is all", "thats all", "that will be all",
                "nothing else", "im done", "i am done", "we are done", "were done")


GUIDED_PATH_CUES = ("run the evaluator demo", "evaluator demo", "guided demo", "demo path",
                    "demo script", "test script",
                    # Asking to be shown round is the same request in ordinary words. Somebody
                    # who has just arrived asks it this way, and a fallback is a poor welcome.
                    "guided tour", "guide me", "show me around", "show me round",
                    "walk me through", "take me through", "give me a tour", "a tour")
NEXT_STEP_CUES = ("what should i try next", "what should we try next", "next step", "what next",
                  "where should i start")
# Going back is a request about the conversation, not about the product, so the platform reads it
# for itself. Only bare phrases: "back to my products" names a destination and stays a request for
# that destination.
BACK_CUES = ("go back", "take me back", "go back please", "back please", "previous screen",
             "the previous screen", "last screen", "the last screen", "where i was",
             "back to where i was", "go back to where i was")
VOICE_CUES = ("voice", "interrupt", "interruption", "stop speaking", "stopping", "listening",
              "listen while", "live voice")

# Every phrase the platform reads for itself. The normalizer keeps these words spelled exactly as
# they are typed, so no word a product declares can pull one of them into a neighbouring spelling
# and leave the platform unable to recognise its own question.
PLATFORM_PHRASES: tuple[str, ...] = (
    *sorted(GREETINGS), *IDENTITY_CUES, *CAPABILITY_CUES, *LAST_CHANGE_CUES, *INTRODUCTION_CUES,
    *THANKS_CUES, *CLOSING_CUES, *GUIDED_PATH_CUES, *NEXT_STEP_CUES, *VOICE_CUES, *BACK_CUES,
)


class Conversational(StrEnum):
    GREETING = "greeting"
    GREETING_NAMED = "greeting_named"
    IDENTITY = "identity"
    CAPABILITIES = "capabilities"
    LAST_CHANGE = "last_change"
    THANKS = "thanks"
    CLOSING = "closing"
    GUIDED_PATH = "guided_path"
    NEXT_STEP = "next_step"
    VOICE_INTERRUPTION = "voice_interruption"


@dataclass(frozen=True)
class ConversationalTurn:
    """A turn the platform answers itself, with the template the definition supplies."""

    kind: Conversational
    template_key: str
    visitor_name: str | None = None


def detect(text: NormalizedMessage, *, visitor_name: str | None = None) -> ConversationalTurn | None:
    """Recognise a conversational turn, or return None and let product routing decide."""
    whole = text.focused.strip()
    if not whole:
        return None

    if whole in GREETINGS:
        if visitor_name:
            # A visitor greeting again after introducing themselves in this session.
            return ConversationalTurn(Conversational.GREETING_NAMED, "greeting_again", visitor_name)
        return ConversationalTurn(Conversational.GREETING, "greeting")

    if any(contains_term(whole, cue) for cue in CAPABILITY_CUES):
        return ConversationalTurn(Conversational.CAPABILITIES, "capabilities")

    if any(contains_term(whole, cue) for cue in IDENTITY_CUES):
        return ConversationalTurn(Conversational.IDENTITY, "identity")

    if any(contains_term(whole, cue) for cue in LAST_CHANGE_CUES):
        return ConversationalTurn(Conversational.LAST_CHANGE, "last_change")

    if any(contains_term(whole, cue) for cue in CLOSING_CUES):
        return ConversationalTurn(Conversational.CLOSING, "conversation_ended")

    if any(contains_term(whole, cue) for cue in THANKS_CUES):
        return ConversationalTurn(Conversational.THANKS, "thanks")

    if _guided_path(whole):
        return ConversationalTurn(Conversational.GUIDED_PATH, "guided_path")

    if _next_step(whole):
        return ConversationalTurn(Conversational.NEXT_STEP, "next_step")

    if _voice_interruption(whole):
        return ConversationalTurn(Conversational.VOICE_INTERRUPTION, "voice_interruption")

    name = _introduced_name(whole, text.original)
    if name is None:
        # "Hi, I'm Priya": a greeting, then an introduction.
        opener = next((g for g in sorted(GREETINGS, key=len, reverse=True) if whole.startswith(g + " ")), None)
        if opener is not None:
            name = _introduced_name(whole[len(opener) + 1:], text.original, greeted=True)
    if name:
        return ConversationalTurn(Conversational.GREETING_NAMED, "greeting_named", name)
    return None


def _guided_path(whole: str) -> bool:
    return any(contains_term(whole, cue) for cue in GUIDED_PATH_CUES)


def _next_step(whole: str) -> bool:
    return any(contains_term(whole, cue) for cue in NEXT_STEP_CUES)


def _voice_interruption(whole: str) -> bool:
    return "voice" in whole and any(contains_term(whole, cue) for cue in VOICE_CUES)


def _introduced_name(whole: str, original: str, *, greeted: bool = False) -> str | None:
    """A name, or nothing. Being wrong here means greeting someone by a word they did not say.

    A capitalized name is always accepted. A lowercase one only where the words can mean nothing
    else: "my name is sam", or a greeting followed by "i am priya". A bare "i am confused" is never
    a name.
    """
    for cue in INTRODUCTION_CUES:
        if not whole.startswith(cue):
            continue
        words = whole[len(cue):].strip(" .!,").split()
        if words and len(words) > 1:
            # "I'm Sam from Acme": the name stops where the rest of the sentence starts.
            cut = next((index for index, word in enumerate(words) if word in _AFTER_NAME), len(words))
            words = words[:cut]
        remainder = " ".join(words)
        if not remainder or len(words) > 2 or not remainder.replace(" ", "").isalpha():
            return None
        if any(word in NOT_NAMES for word in words):
            return None
        lowercase_allowed = cue in EXPLICIT_NAME_CUES or (greeted and len(words) == 1)
        if not lowercase_allowed and not _capitalized_in(original, words):
            # People capitalize their own names. "i am ready" is not an introduction.
            return None
        return remainder.title()
    return None


def _capitalized_in(original: str, words: Iterable[str]) -> bool:
    """Every word of the candidate name appears capitalized in what the visitor actually typed."""
    typed = {word.strip(".,!?"): word.strip(".,!?") for word in original.split()}
    for word in words:
        match = next((typed[key] for key in typed if key.lower() == word), None)
        if match is None or not match[:1].isupper():
            return False
    return True


@dataclass(frozen=True)
class OfferableActions:
    """What the assistant may honestly say it can do for this caller, on this deployment."""

    keys: tuple[str, ...]
    descriptions: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.keys


@dataclass(frozen=True)
class CapabilityPolicy:
    """Who may do what, and what the installed adapter can actually express.

    Both questions are required. An optional filter fails *open*: forget to pass it and the
    assistant advertises every declared action, including ones no adapter can carry out and ones
    this caller may not use. A policy object makes both answers explicit at the call site.
    """

    translatable: Callable[[str], bool]
    permitted: Callable[[str], bool]

    @classmethod
    def nothing(cls) -> "CapabilityPolicy":
        """Offer nothing. The safe default when neither answer is known yet."""
        return cls(translatable=lambda key: False, permitted=lambda key: False)


def offerable(
    definition: ProductDefinition,
    snapshot: TurnSnapshot,
    policy: CapabilityPolicy,
) -> OfferableActions:
    """The actions the assistant may offer, after four honest filters.

    An action is offered only when it is: declared by this product; expressible by the installed
    adapter; permitted for this caller; and reachable under the caller's current scope — an action
    over an entity with no visible records is not something it can do for them right now.
    """
    keys: list[str] = []
    descriptions: list[str] = []
    # In the order the definition declares them. A product decides what matters most about
    # itself, and an alphabetical list buries it: "audit" should not be offered before
    # "add a product" because of its first letter.
    for key, spec in definition.actions.items():
        if not policy.translatable(key):
            continue
        if not policy.permitted(key):
            continue
        if spec.entity and snapshot.count(spec.entity) == 0 and str(spec.capability) != "CREATE_RECORD":
            # Nothing of that kind is visible, so offering to show *or change* it would be a
            # promise it cannot keep. Creating is the one thing still possible on an empty scope.
            continue
        keys.append(key)
        descriptions.append(action_description(definition, key))
    return OfferableActions(tuple(keys), tuple(descriptions))


# The order offers are spoken in: what changes things first, then where to look.
_SPOKEN_ORDER = {
    Capability.CREATE_RECORD: 0, Capability.UPDATE_RECORD: 1, Capability.OPEN_RECORD: 2,
    Capability.FILTER_RECORDS: 3, Capability.NAVIGATE_VIEW: 4, Capability.HIGHLIGHT_CONTROL: 5,
}


def capability_sentence(offers: OfferableActions, definition: ProductDefinition, *, limit: int = 6) -> str:
    """Regenerate from declared operations; cached or product-written descriptions are not speech."""
    ordered = sorted(offers.keys, key=lambda key: _SPOKEN_ORDER[definition.actions[key].capability])
    chosen: list[str] = []
    for key in ordered:
        description = action_description(definition, key)
        if description not in chosen:
            chosen.append(description)
    return _spoken_list(chosen[:limit], "and")


def guided_steps(offers: OfferableActions, definition: ProductDefinition) -> str:
    """A short route through what this caller can actually do: two places to look, one thing to
    change, one control to find, then the guardrail. Drawn from the filtered offers only, so it
    never names a record, a person or anything the caller cannot reach."""
    by_capability: dict[Capability, list[str]] = {}
    for key in offers.keys:
        spec = definition.actions[key]
        if spec.capability == Capability.NAVIGATE_VIEW and (
            spec.view not in definition.views or definition.views[spec.view].kind == "dashboard"
        ):
            continue
        if spec.capability == Capability.HIGHLIGHT_CONTROL:
            # The route is what the visitor does, so a control is something to find.
            view = definition.views[spec.view]
            step = f"find {_plain_name(view.controls[spec.control].label)} in {_plain_name(view.label)}"
        else:
            step = action_description(definition, key)
        by_capability.setdefault(spec.capability, []).append(step)
    # A route starts at the front door. Record screens make the most of a tour, so they fill it,
    # but the place a product declares first is where its author means somebody to begin - and
    # for a product whose screens are all dashboards it is the only route there is.
    places = by_capability.get(Capability.NAVIGATE_VIEW, [])
    first = next((action_description(definition, key) for key in offers.keys
                  if definition.actions[key].capability == Capability.NAVIGATE_VIEW), None)
    if first is not None and first not in places:
        places = [first, *places]
    steps = [
        *places[:3],
        *by_capability.get(Capability.CREATE_RECORD, [])[:1],
        *by_capability.get(Capability.HIGHLIGHT_CONTROL, [])[:1],
    ]
    steps.append(f"ask me for something outside {definition.identity.product_name} to see how I stay in scope")
    return _spoken_list(steps, "then", serial=True)


def action_description(definition: ProductDefinition, key: str) -> str:
    """The operation determines the verb; the definition's labels name the things, plainly."""
    spec = definition.actions[key]
    if spec.capability == Capability.NAVIGATE_VIEW:
        if spec.view == "architecture":
            return "show the system architecture"
        return f"open {_plain_name(definition.views[spec.view].label)}"
    if spec.capability == Capability.HIGHLIGHT_CONTROL:
        view = definition.views[spec.view]
        return (f"show you where {_plain_name(view.controls[spec.control].label)} is in "
                f"{_plain_name(view.label)}")
    entity = definition.entities[spec.entity]
    label = _plain_name(entity.label).lower()
    if spec.capability == Capability.OPEN_RECORD:
        return f"open {_a(label)}"
    if spec.capability == Capability.FILTER_RECORDS:
        return f"list {_plain_name(entity.plural).lower()} by {_field_name(spec.by)}"
    if spec.capability == Capability.CREATE_RECORD:
        return f"create {_a(label)}"
    if spec.capability == Capability.UPDATE_RECORD:
        fields = [_field_name(name) for name in sorted(spec.fields)]
        return f"change {_a(label)}'s {_spoken_list(fields, 'or')}"
    raise ValueError(f'no platform description for {spec.capability}')


def _a(noun: str) -> str:
    """The noun with its article. A product names its own things, so the article follows the
    label rather than being written into the sentence: "an invoice", never "a invoice"."""
    return f"an {noun}" if noun[:1].lower() in "aeiou" else f"a {noun}"


def _field_name(name: str | None) -> str:
    return (name or "").replace("_", " ")


def _spoken_list(items: list[str], joiner: str, *, serial: bool = False) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if serial:
        return f", {joiner} ".join(items)
    return ", ".join(items[:-1]) + f" {joiner} {items[-1]}"


def _plain_name(label: str) -> str:
    """Product labels are spoken as plain names, and only once they pass the platform's name check."""
    if name_problems(label):
        raise ValueError('capability labels must be plain names')
    return label

# The reply for a question no installed knowledge source can answer. Knowledge availability is
# asserted by the platform, so this is always platform wording, however a definition words it.
KNOWLEDGE_UNAVAILABLE_KEY = "knowledge_unavailable"
