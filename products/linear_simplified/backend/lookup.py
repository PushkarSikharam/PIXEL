"""This product's record lookup over the live record store (3.2 plan, section 7).

The lookup is built for one caller and bound to the workspaces that caller may see. The scope
is applied when records are loaded, before anything is matched, counted or offered as a choice,
so a record outside it is indistinguishable from one that does not exist. Nothing here can
widen the scope: it is fixed at construction and never read from a message.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.engine.lookup import PeopleMatch, PersonView, RecordView
from app.record_access import RecordGrant
from app.services.product_data_store import ProductDataStore

# The entities this product's definition declares, and where each one's records come from.
ENTITY_SOURCES = {"issue": "issues", "project": "projects", "cycle": "cycles", "member": "team"}
PERSON_FIELDS = {"issue": "assignee", "project": "lead"}


def person_id(name: str) -> str:
    """People are referred to by a stable ID derived from the name the records carry."""
    return name.lower().replace(" ", "-")


class LinearLegacyLookup:
    """Read-only, scope-bound. Built by `lookup_for`, never directly from a request."""

    def __init__(self, store: ProductDataStore, visible_scope_ids: frozenset[str] | None) -> None:
        self._store = store
        self._scope_ids = visible_scope_ids

    def _data(self) -> Mapping[str, list[dict[str, Any]]]:
        # Scope first: the store filters before we see anything.
        return self._store.load(self._scope_ids)

    def _records(self, entity: str) -> list[RecordView]:
        source = ENTITY_SOURCES.get(entity)
        if source is None:
            return []
        rows = self._data().get(source, [])
        return [view for view in (self._view(entity, row) for row in rows) if view is not None]

    def _view(self, entity: str, row: dict[str, Any]) -> RecordView | None:
        if entity == "issue":
            return RecordView("issue", row["id"], row.get("title") or "", {
                "assignee": person_id(row.get("assignee") or ""),
                "priority": row.get("priority"),
                "status": row.get("status"),
                "project": row.get("projectId") or row.get("project"),
            })
        if entity == "project":
            return RecordView("project", row["id"], row.get("name") or "", {
                "lead": person_id(row.get("lead") or ""),
                "status": row.get("status"),
            })
        if entity == "cycle":
            return RecordView("cycle", row["id"], row.get("name") or "", {
                "project": row.get("projectId"), "status": row.get("status"),
            })
        if entity == "member":
            name = row.get("name") or ""
            return RecordView("member", person_id(name), name, {
                "projects": tuple(row.get("projectIds") or ()), "role": row.get("role"),
            })
        return None

    def get(self, entity: str, record_id: str) -> RecordView | None:
        return next((r for r in self._records(entity) if r.id.lower() == record_id.lower()), None)

    def search(self, entity: str, text: str, limit: int) -> list[RecordView]:
        needle = text.lower().strip()
        if not needle:
            return []
        return [r for r in self._records(entity) if needle in r.title.lower()][:limit]

    def by_person(self, entity: str, person_id: str, limit: int) -> list[RecordView]:
        field = PERSON_FIELDS.get(entity)
        if field is None:
            return []
        return [r for r in self._records(entity) if r.fields.get(field) == person_id][:limit]

    def people(self, text: str, limit: int) -> PeopleMatch:
        wanted = text.lower().split()
        if not wanted:
            return PeopleMatch()
        matches = tuple(
            PersonView(r.id, r.title) for r in self._records("member")
            if r.title.lower().split() == wanted
            or (len(wanted) == 1 and wanted[0] in r.title.lower().split())
        )
        return PeopleMatch(matches[:limit])

    def count(self, entity: str) -> int:
        return len(self._records(entity))


    # --- Snapshot source (3.2 plan, section 7.1) ---

    @property
    def scope_label(self) -> str:
        return "all-workspaces" if self._scope_ids is None else ",".join(sorted(self._scope_ids))

    def materialize(self, connection) -> Mapping[str, tuple[RecordView, ...]]:
        """Read every visible record for one turn, inside the caller's read transaction.

        One `load` call, so every entity in the snapshot comes from the same moment. The
        connection is not kept: the snapshot holds records, never a database handle.
        """
        data = self._store.load(self._scope_ids, connection=connection)
        materialized: dict[str, tuple[RecordView, ...]] = {}
        for entity, source in ENTITY_SOURCES.items():
            views = (self._view(entity, row) for row in data.get(source, []))
            materialized[entity] = tuple(view for view in views if view is not None)
        return materialized


def lookup_for(grant: RecordGrant, store: ProductDataStore | None = None) -> LinearLegacyLookup:
    """Bind a lookup to exactly what this record grant may see.

    A visitor's grant carries that visitor's private demo instance, and the lookup reads only it.
    Omitting the store must never fall back to the shared member records, and a store for a
    different instance (or the shared store for a visitor) is refused rather than trusted.
    """
    if store is None:
        store = ProductDataStore(grant.demo_context) if grant.demo_context else ProductDataStore()
    elif store.demo_context != grant.demo_context:
        raise ValueError("the record store does not belong to this grant's demo instance")
    return LinearLegacyLookup(store, grant.visible_scope_ids())


# What the engine needs in order to resolve people inside a snapshot of this product.
PEOPLE_ENTITY = "member"
