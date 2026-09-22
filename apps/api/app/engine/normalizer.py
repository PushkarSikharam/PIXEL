"""Message normalization driven by the definition's vocabulary.

The original message is always kept. Two normalized forms are produced:

- `full` keeps every word of the message (lower case, punctuation removed, spelling fixed).
  Refusal checks use it, so a correction marker or a negation can never hide a prohibited
  request ("delete everything, actually show the list" is still refused).
- `focused` is what the visitor finally asked for: the clause after the last correction marker,
  with negated product terms removed. Intent matching uses it.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import get_close_matches

from app.definitions.contract import Vocabulary

_POSSESSIVE = re.compile(r"(?<=[a-z0-9])['’]s\b")
_APOSTROPHES = re.compile(r"['’]")
_NOT_WORD = re.compile(r"[^a-z0-9-]+")
_LOOSE_HYPHENS = re.compile(r"(?<![a-z0-9])-|-(?![a-z0-9])")
_SPACES = re.compile(r"\s+")
_NEGATIONS = ("do not", "dont", "not", "no")
# Words this short are never spelling-corrected.
_MIN_CORRECTABLE = 4
# Function words are never spelling-corrected, however close they are to a product term.
_NEVER_CORRECTED = frozenset({
    "dont", "does", "done", "from", "have", "here", "into", "just", "like", "make", "mark",
    "more", "most", "need", "none", "nope", "okay", "only", "open", "over", "please", "show",
    "some", "sure", "than", "that", "them", "then", "they", "this", "what", "when", "where",
    "which", "with", "wrong", "your",
})
_FUZZY_CUTOFF = 0.84


@dataclass(frozen=True)
class NormalizedMessage:
    original: str
    full: str
    focused: str

    @property
    def words(self) -> tuple[str, ...]:
        """Every word of the message, spelling fixed. Used where context must not be lost."""
        return tuple(self.full.split())

    @property
    def focused_words(self) -> tuple[str, ...]:
        """The words of the corrected request only.

        What the visitor asks for is decided from these: after "status CON-1, actually status
        CON-2", the request is about CON-2. Safety checks keep using `full`.
        """
        return tuple(self.focused.split())


class Normalizer:
    def __init__(
        self, vocabulary: Vocabulary, known_words: Iterable[str] = (), protected_phrases: Iterable[str] = (),
    ) -> None:
        # Longest phrases first, so "git hub" is replaced before "hub" could be.
        self._corrections = sorted(vocabulary.corrections.items(), key=lambda item: -len(item[0]))
        self._markers = tuple(vocabulary.correction_markers)
        self._negatable = tuple(sorted(vocabulary.negatable_terms, key=len, reverse=True))
        words = {word for term in vocabulary.terms for word in term.split()}
        words.update(word for term in known_words for word in term.split())
        self._known = frozenset(words)
        self._fuzzy_pool = sorted(word for word in self._known if len(word) >= _MIN_CORRECTABLE)
        # Words the platform itself reads (markers, negations, replies) keep their exact form.
        self._protected = frozenset(
            word for phrase in (*self._markers, *_NEGATIONS, *protected_phrases) for word in phrase.split()
        )

    def normalize(self, message: str) -> NormalizedMessage:
        full = self._spell(self._basic(message))
        return NormalizedMessage(original=message, full=full, focused=self._focus(full) or full)

    def _basic(self, message: str) -> str:
        text = _POSSESSIVE.sub("", message.lower())
        text = _APOSTROPHES.sub("", text)
        text = _NOT_WORD.sub(" ", text)
        text = _LOOSE_HYPHENS.sub(" ", text)
        text = _collapse(text)
        for source, target in self._corrections:
            text = _replace_term(text, source, target)
        return text

    def _spell(self, text: str) -> str:
        corrected = []
        for word in text.split():
            if (word in self._known or word in _NEVER_CORRECTED or word in self._protected
                    or len(word) < _MIN_CORRECTABLE or any(ch.isdigit() for ch in word)):
                corrected.append(word)
                continue
            match = get_close_matches(word, self._fuzzy_pool, n=1, cutoff=_FUZZY_CUTOFF)
            corrected.append(match[0] if match else word)
        return " ".join(corrected)

    def _focus(self, text: str) -> str:
        selected = text
        for marker in self._markers:
            matches = list(_term_pattern(marker).finditer(selected))
            if matches:
                match = matches[-1]
                tail = selected[match.end():].strip()
                selected = tail if tail else selected[:match.start()].strip()
        words = selected.split()
        if len(words) > 2 and words[0] == "no":
            selected = " ".join(words[1:])
        for negation in _NEGATIONS:
            for term in self._negatable:
                selected = re.sub(
                    rf"(?<![a-z0-9-]){re.escape(negation)}\s+(?:the\s+)?{re.escape(term)}(?![a-z0-9-])",
                    " ", selected,
                )
        return _collapse(selected)


def contains_term(text: str, term: str) -> bool:
    """Whole-word (or whole-phrase) literal match."""
    return _term_pattern(term).search(text) is not None


def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9-]){re.escape(term)}(?![a-z0-9-])")


def _replace_term(text: str, source: str, target: str) -> str:
    return _term_pattern(source).sub(target, text)


def _collapse(text: str) -> str:
    return _SPACES.sub(" ", text).strip()
