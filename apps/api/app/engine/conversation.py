"""Platform conversation detection and descriptions of offerable operations.

Detection is generic. A capability's verb, target, and allowed fields come from its contract;
product-authored action descriptions are not evidence of what the assistant can execute.
Adapter support, caller permissions, and visible records restrict the offered actions.
"""
from __future__ import annotations

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
)
# "I'm Priya" and similar. Matched against normalized text, which has had apostrophes removed,
# so the cue is "im " rather than "i'm ". Cues that ordinary sentences start with ("call me back
# later", "this is urgent") are deliberately absent: they produced names like "Back Later".
INTRODUCTION_CUES = ("i am ", "im ", "my name is ")

# Words that follow an introduction cue without being a name.
NOT_NAMES = frozenset({
    "looking", "here", "trying", "just", "not", "sure", "interested", "new", "back", "done",
    "ready", "good", "fine", "ok", "okay", "working", "wondering", "curious", "the", "a", "an",
})


class Conversational(StrEnum):
    GREETING = "greeting"
    GREETING_NAMED = "greeting_named"
    IDENTITY = "identity"
    CAPABILITIES = "capabilities"


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
            return ConversationalTurn(Conversational.GREETING_NAMED, "greeting_named", visitor_name)
        return ConversationalTurn(Conversational.GREETING, "greeting")

    if any(contains_term(whole, cue) for cue in IDENTITY_CUES):
        return ConversationalTurn(Conversational.IDENTITY, "identity")

    if any(contains_term(whole, cue) for cue in CAPABILITY_CUES):
        return ConversationalTurn(Conversational.CAPABILITIES, "capabilities")

    name = _introduced_name(whole, text.original)
    if name is None:
        # "Hi, I'm Priya": a greeting, then an introduction.
        opener = next((g for g in sorted(GREETINGS, key=len, reverse=True) if whole.startswith(g + " ")), None)
        if opener is not None:
            name = _introduced_name(whole[len(opener) + 1:], text.original)
    if name:
        return ConversationalTurn(Conversational.GREETING_NAMED, "greeting_named", name)
    return None


def _introduced_name(whole: str, original: str) -> str | None:
    """A name, or nothing. Being wrong here means greeting someone by a word they did not say."""
    for cue in INTRODUCTION_CUES:
        if not whole.startswith(cue):
            continue
        remainder = whole[len(cue):].strip(" .!,")
        words = remainder.split()
        if not remainder or len(words) > 2 or not remainder.replace(" ", "").isalpha():
            return None
        if any(word in NOT_NAMES for word in words):
            return None
        if not _capitalized_in(original, words):
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
    for key, spec in sorted(definition.actions.items()):
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


def capability_sentence(offers: OfferableActions, definition: ProductDefinition, *, limit: int = 6) -> str:
    """Regenerate from declared operations; cached or product-written descriptions are not speech."""
    chosen = [action_description(definition, key) for key in offers.keys[:limit]]
    if not chosen:
        return ""
    if len(chosen) == 1:
        return chosen[0]
    return ", ".join(chosen[:-1]) + f" and {chosen[-1]}"


def action_description(definition: ProductDefinition, key: str) -> str:
    """The operation determines the verb. Product nouns are explicitly presented as names."""
    spec = definition.actions[key]
    if spec.capability == Capability.NAVIGATE_VIEW:
        label = 'System architecture' if spec.view == 'architecture' else definition.views[spec.view].label
        return f'open the {_quoted_name(label)} view'
    if spec.capability == Capability.HIGHLIGHT_CONTROL:
        view = definition.views[spec.view]
        return f'highlight {_quoted_name(view.controls[spec.control].label)} in the {_quoted_name(view.label)} view'
    label = _quoted_name(definition.entities[spec.entity].label)
    if spec.capability == Capability.OPEN_RECORD:
        return f'open a {label} record'
    if spec.capability == Capability.FILTER_RECORDS:
        return f'filter {label} records by "{spec.by}"'
    fields = ', '.join(f'"{field}"' for field in sorted(spec.fields))
    if spec.capability == Capability.CREATE_RECORD:
        return f'create a {label} record with {fields}'
    if spec.capability == Capability.UPDATE_RECORD:
        return f'update {fields} on a {label} record'
    raise ValueError(f'no platform description for {spec.capability}')


def _quoted_name(label: str) -> str:
    if name_problems(label):
        raise ValueError('capability labels must be plain names')
    return f'"{label}"'


# The reply for a question no installed knowledge source can answer. Knowledge availability is
# asserted by the platform, so this is always platform wording, however a definition words it.
KNOWLEDGE_UNAVAILABLE_KEY = "knowledge_unavailable"
