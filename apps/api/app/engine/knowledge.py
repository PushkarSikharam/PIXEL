"""The knowledge boundary (3.2 plan, section 7.2).

Structured records reach the engine through `RecordLookup`. Product *documents* reach it through
this, and on the same terms: read-only, bound to one caller's scope when it is built, and
implemented by the product package. Core never imports a product's retriever.

Two rules make this safe to answer from:

- **Nothing is installed by default.** `NoKnowledge` is the fallback, and it finds nothing. A
  deployment with no knowledge source cannot accidentally answer from a stale or foreign one.
- **An answer is grounded or it is not given.** When no passage is found, the platform uses a
  fixed honest fallback. It never fills the gap with a guess, and it never implies the question
  was out of scope when the truth is that nothing was installed.

Indexing, versioning and ranking are Milestone 3.4. This is only the seam, so that dismantling
today's assistant does not silently drop the documents a reply was grounded in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.definitions.safety import check_key, check_slug, check_text


_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class KnowledgeContext:
    """Who is asking, and which product's knowledge at which version they may read.

    A knowledge source is built for one of these and never sees another. Today's documents are
    static and shipped with a definition, so an implementation may not need every field — but the
    boundary carries them now, so that tenant-scoped knowledge in Milestone 3.4 is a change of
    implementation rather than a change of shape.
    """

    tenant_id: str
    product_id: str
    definition_id: str
    definition_version: int
    definition_checksum: str
    knowledge_version: int
    scope_label: str

    def __post_init__(self) -> None:
        for name in ("tenant_id", "product_id"):
            check_slug(getattr(self, name))
        check_key(self.definition_id)
        for name in ("definition_version", "knowledge_version"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not _SHA256.fullmatch(self.definition_checksum):
            raise ValueError("definition_checksum must be a lowercase sha256 digest")
        check_text(self.scope_label)


@dataclass(frozen=True)
class KnowledgePassage:
    """One passage a reply may be grounded in. Content is a snippet, never a whole document."""

    title: str
    source: str
    snippet: str
    # Whether the passage really matches the question, so it may be quoted as the answer. A
    # knowledge source that cannot tell leaves it True.
    grounds_answer: bool = True

    def __post_init__(self) -> None:
        for name in ("title", "source", "snippet"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} must be text")
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "source", self.source.strip())
        object.__setattr__(self, "snippet", self.snippet.strip())


@runtime_checkable
class KnowledgeLookup(Protocol):
    """A product's documents, already narrowed to what this caller may read.

    `search` takes no scope, tenant or product: those are fixed when the lookup is built, exactly
    as they are for `RecordLookup`. A caller cannot widen what it sees by asking differently.
    """

    def search(self, text: str, limit: int) -> list[KnowledgePassage]: ...


class NoKnowledge:
    """The default: no knowledge source is installed, so nothing is ever found.

    This is deliberately not an error. A product without documents is a valid product; it simply
    cannot answer knowledge questions, and the platform says so rather than improvising.
    """

    def search(self, text: str, limit: int) -> list[KnowledgePassage]:
        return []


@dataclass(frozen=True)
class Grounding:
    """What a knowledge answer may be built from, and whether it may be given at all.

    Frozen all the way down: a caller that keeps a reference to the list it passed in cannot
    change whether an answer is grounded afterwards.
    """

    passages: tuple[KnowledgePassage, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "passages", tuple(self.passages))
        for passage in self.passages:
            if not passage.source.strip() or not passage.snippet.strip():
                raise ValueError("a passage needs both a source and a snippet")

    @property
    def is_grounded(self) -> bool:
        return bool(self.passages)

    @property
    def sources(self) -> tuple[str, ...]:
        # Order preserved, duplicates removed: one citation per document.
        seen: dict[str, None] = {}
        for passage in self.passages:
            seen.setdefault(passage.source, None)
        return tuple(seen)


def ground(lookup: KnowledgeLookup, question: str, *, limit: int = 2) -> Grounding:
    """Find passages for a question. An empty result is an answer in itself: do not answer."""
    if not question.strip():
        return Grounding()
    found = lookup.search(question, limit)
    return Grounding(tuple(found[:limit]))


def answerable(grounding: Grounding) -> Grounding:
    """Only passages that really match the question may be quoted as its answer.

    Supporting passages attached to an action reply are never spoken, so any match will do there;
    a quoted answer needs a passage the product's knowledge source marks as a real match.
    """
    return Grounding(tuple(passage for passage in grounding.passages if passage.grounds_answer))
