"""What a message mentions: names, record IDs, field values and titles.

These are platform heuristics for English messages. Everything they find is checked against
the caller's scope-bound `RecordLookup` before it is used; a mention on its own grants nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.definitions.contract import EntitySpec
from app.engine.normalizer import contains_term

# Words after which a name usually follows. Names that cannot be found must also be capitalized.
NAME_CUES = frozenset({"for", "to"})
# Words after which the rest of the message is a subject, never a name ("... about Login errors").
SUBJECT_CUES = frozenset({"about"})
# Common words that are never names, whatever their capitalization.
COMMON_WORDS = frozenset({
    "a", "about", "all", "an", "and", "any", "are", "at", "can", "could", "demo", "do", "does",
    "every", "for", "from", "hello", "help", "hey", "hi", "how", "i", "im", "in", "is", "it",
    "me", "my", "new", "of", "on", "open", "or", "please", "show", "some", "that", "the", "this",
    "to", "us", "we", "what", "when", "where", "who", "why", "with", "you", "your",
    # Contractions, compared without their apostrophe.
    "dont", "doesnt", "id", "ill", "im", "isnt", "its", "ive", "lets", "thats", "theyre", "were",
    "whats", "youre",
})
_TOKEN = re.compile(r"[A-Za-z][A-Za-z'’-]*")
_POSSESSIVE = re.compile(r"['’]s?$")
_APOSTROPHE = re.compile(r"['’]")
_RECORD_ID = re.compile(r"^[a-z]+-\d+$")
_TITLE_CUE = re.compile(r"\babout\s+(.+)$", re.IGNORECASE)


@dataclass(frozen=True)
class NameMention:
    text: str  # as the visitor wrote it, for replies ("Priya Shah")

    @property
    def words(self) -> tuple[str, ...]:
        return tuple(self.text.lower().split())


def name_mentions(
    original: str, known_words: frozenset[str], *, subjects_are_names: bool = False,
) -> list[NameMention]:
    """Phrases that read like names of people who may not exist: capitalized words that are not
    the first word, not common or product words, and not part of a subject ("about ...").

    Visible people are found separately, by looking up every word; this only decides which
    unmatched words are worth reporting as "not found". `subjects_are_names` is for a message
    already known to be about a person ("what about <person>" as a follow-up), where the word after
    "about" is the name rather than a topic.
    """
    raw_tokens = _TOKEN.findall(original)
    tokens = [_POSSESSIVE.sub("", token) for token in raw_tokens]
    mentions: list[NameMention] = []
    current: list[str] = []
    in_subject = False
    for index, token in enumerate(tokens):
        word = _APOSTROPHE.sub("", raw_tokens[index]).lower()  # "I'm" -> "im"
        if word in SUBJECT_CUES and not subjects_are_names:
            in_subject = True
        is_name = (
            not in_subject
            and index > 0
            and len(word) >= 2
            and word not in COMMON_WORDS
            and token.lower() not in COMMON_WORDS
            and token.lower() not in known_words
            and token[0].isupper()
        )
        if is_name:
            current.append(token)
        elif current:
            mentions.append(NameMention(" ".join(current)))
            current = []
    if current:
        mentions.append(NameMention(" ".join(current)))
    return mentions


def person_search_words(words: tuple[str, ...], known_words: frozenset[str]) -> list[str]:
    """Words worth looking up as people, including a possessive written without an apostrophe."""
    candidates: list[str] = []
    for word in words:
        if word in COMMON_WORDS or word in known_words or len(word) < 2 or not word.isalpha():
            continue
        candidates.append(word)
        if word.endswith("s") and len(word) > 3:
            candidates.append(word[:-1])
    return candidates


def record_ids(words: tuple[str, ...]) -> list[str]:
    return [word.upper() for word in words if _RECORD_ID.match(word)]


def enum_values(entity: EntitySpec, field_names: list[str], text: str) -> dict[str, list[str]]:
    """For each enum field, the declared values the message names."""
    found: dict[str, list[str]] = {}
    for name in field_names:
        spec = entity.fields.get(name)
        if spec is None or spec.type != "enum":
            continue
        values = [value for value in sorted(spec.values or [], key=len, reverse=True) if contains_term(text, value.lower())]
        # A longer value that contains a shorter one wins ("in progress" over "progress").
        values = [value for value in values if not any(value != other and value.lower() in other.lower() for other in values)]
        if values:
            found[name] = values
    return found


def title_text(original: str) -> str | None:
    """The subject of a new record, taken from "... about <subject>"."""
    match = _TITLE_CUE.search(original)
    if not match:
        return None
    subject = re.sub(r"[^\w\s'-]", "", match.group(1)).strip()
    return subject[:1].upper() + subject[1:] if subject else None
