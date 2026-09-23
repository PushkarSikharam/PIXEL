"""A second neutral product, unrelated to the sample desk, for holding core to more than one.

Pixel's claim is that one platform runs many products. A single product in the tests cannot show
that: anything the core quietly assumed about it would look correct. This fixture is deliberately
unlike the desk — different entities, different words, different views, different guardrails — so
a core change that suits one product and breaks the other fails here rather than in someone's
onboarding.

It is a fixture, never a shipped product: nothing installs it, nothing seeds it, and the demo
still ships exactly one product.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.engine.lookup import PeopleMatch, PersonView, RecordView

DEFINITION_ID = "sample_library"


def library_definition(version: int = 1) -> dict:
    return {
        "definition": {"definition_id": DEFINITION_ID, "version": version,
                       "ownership": "platform_shared"},
        "identity": {
            "product_name": "Sample Library",
            "assistant_name": "Guide",
            "persona": "A concise guide for a lending library.",
            "voice_style": "Calm and clear.",
            "greeting": "Welcome to {product}. I'm {assistant}.",
        },
        "vocabulary": {
            "terms": ["book", "books", "catalogue", "loan", "loans", "librarian", "librarians",
                      "shelf", "borrowed"],
            "corrections": {"boook": "book", "libarian": "librarian"},
            "correction_markers": ["actually", "instead"],
            "negatable_terms": ["book", "books", "librarian", "librarians"],
        },
        "entities": {
            "book": {
                "label": "Book", "plural": "Books",
                "id": {"strategy": "prefix", "prefix": "BK"},
                "title_field": "title",
                "summary_fields": ["status"],
                "fields": {
                    "title": {"type": "text", "required": True, "max": 200},
                    "status": {"type": "enum", "required": True, "values": ["On shelf", "On loan"],
                               "default": "On shelf"},
                    "keeper": {"type": "ref", "target": "librarian", "required": True},
                },
            },
            "librarian": {
                "label": "Librarian", "plural": "Librarians",
                "id": {"strategy": "slug", "from_field": "name"},
                "title_field": "name",
                "fields": {
                    "name": {"type": "text", "required": True, "editable": False},
                    "books": {"type": "refs", "target": "book"},
                },
            },
        },
        "people": {"entity": "librarian", "assigned_by": ["book.keeper"], "match_on": ["name"]},
        "scope": {"anchor": "book", "paths": {"book": [], "librarian": ["books"]}},
        "views": {
            "catalogue": {"label": "Catalogue", "kind": "list", "entity": "book",
                          "columns": ["title", "status"]},
            "librarians": {"label": "Librarians", "kind": "list", "entity": "librarian",
                           "columns": ["name"]},
        },
        "actions": {
            "open_catalogue": {"capability": "NAVIGATE_VIEW", "view": "catalogue",
                               "description": "Open the catalogue."},
            "open_librarians": {"capability": "NAVIGATE_VIEW", "view": "librarians",
                                "description": "Open the librarian directory."},
            "open_book": {"capability": "OPEN_RECORD", "entity": "book",
                          "description": "Open one book."},
            "books_by_keeper": {"capability": "FILTER_RECORDS", "entity": "book", "by": "keeper",
                                "description": "Show every book one librarian keeps."},
            "add_book": {"capability": "CREATE_RECORD", "entity": "book",
                         "fields": ["title", "status", "keeper"],
                         "description": "Add a book."},
            "reassign_book": {"capability": "UPDATE_RECORD", "entity": "book",
                              "fields": ["keeper", "status"],
                              "description": "Change who keeps a book, or whether it is out."},
            "add_librarian": {"capability": "CREATE_RECORD", "entity": "librarian",
                              "fields": ["name"], "description": "Add a librarian."},
        },
        "intents": [
            {"action": "open_catalogue", "response": "anchor_count",
             "match": [["how many", "count", "number of"], ["book", "books"]]},
            {"action": "open_catalogue", "response": "view_opened",
             "match": [["book", "books", "catalogue", "shelf"]]},
            {"action": "open_librarians", "response": "view_opened",
             "match": [["librarian", "librarians"]]},
            {"action": "books_by_keeper", "requires": ["person"], "response": "records_filtered",
             "match": [["kept by", "for"], ["books"]]},
            {"action": "open_book", "requires": ["record"], "response": "record_opened",
             "match": [["open", "show", "pull up"]]},
            {"action": "add_book", "requires": ["person"], "response": "record_created",
             "match": [["add", "create", "new"], ["book"]]},
            {"action": "add_librarian", "response": "record_created",
             "match": [["add", "create", "new"], ["librarian"]]},
            {"action": "reassign_book", "requires": ["record"], "response": "record_updated",
             "match": [["give", "hand", "keeper", "assign", "on loan", "on shelf"]]},
        ],
        "guardrails": [
            {"topic": "destructive_change", "response": "destructive_refused",
             "match": [["delete", "erase", "wipe", "remove all"]]},
        ],
        "responses": {
            "view_opened": "I'll open {view}.",
            "anchor_count": "{scope} has {count} visible books: {records}.",
            "record_opened": "I'll open {record_id}.",
            "records_filtered": "I found {count} books kept by {person}.",
            "destructive_refused": "I can't delete or erase anything here.",
            "record_created": "I'll add the book.",
            "record_updated": "I'll update {record_id}: {changes}.",
            "clarify_create": "What would you like to add?",
        },
    }


@dataclass
class SampleLibrary:
    """Two shelves; the caller may see only what is on the open shelf."""

    books: dict[str, tuple[RecordView, str]] = field(default_factory=lambda: {
        "BK-1": (RecordView("book", "BK-1", "Tide Tables", {"status": "On shelf", "keeper": "rosa-vale"}), "open"),
        "BK-2": (RecordView("book", "BK-2", "Winter Almanac", {"status": "On loan", "keeper": "rosa-vale"}), "open"),
        "BK-3": (RecordView("book", "BK-3", "Reserved Folio", {"status": "On shelf", "keeper": "otto-lind"}), "closed"),
    })
    librarians: dict[str, tuple[str, str]] = field(default_factory=lambda: {
        "rosa-vale": ("Rosa Vale", "open"),
        "otto-lind": ("Otto Lind", "closed"),
    })


class LibraryLookup:
    """Scope is applied first; a book on the closed shelf behaves as if it did not exist."""

    def __init__(self, library: SampleLibrary, visible_shelves: frozenset[str]) -> None:
        self._library = library
        self._visible = visible_shelves

    def _records(self, entity: str) -> list[RecordView]:
        if entity == "book":
            return [book for book, shelf in self._library.books.values() if shelf in self._visible]
        if entity == "librarian":
            return [
                RecordView("librarian", key, name, {"shelf": shelf})
                for key, (name, shelf) in self._library.librarians.items() if shelf in self._visible
            ]
        return []

    def get(self, entity: str, record_id: str) -> RecordView | None:
        return next((r for r in self._records(entity) if r.id.lower() == record_id.lower()), None)

    def search(self, entity: str, text: str, limit: int) -> list[RecordView]:
        needle = text.lower().strip()
        return [r for r in self._records(entity) if needle and needle in r.title.lower()][:limit]

    def by_person(self, entity: str, person_id: str, limit: int) -> list[RecordView]:
        return [r for r in self._records(entity) if r.fields.get("keeper") == person_id][:limit]

    def people(self, text: str, limit: int) -> PeopleMatch:
        wanted = text.lower().split()
        matches = tuple(
            PersonView(r.id, r.title) for r in self._records("librarian")
            if wanted and (r.title.lower().split() == wanted
                           or (len(wanted) == 1 and wanted[0] in r.title.lower().split()))
        )
        return PeopleMatch(matches[:limit])

    def count(self, entity: str) -> int:
        return len(self._records(entity))

    @property
    def scope_label(self) -> str:
        return "the open shelf" if self._visible == frozenset({"open"}) else "every shelf"
