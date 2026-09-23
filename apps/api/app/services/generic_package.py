"""Running a product that ships no code of its own (3.5).

Until now every product needed an installed Python package - a record lookup and an action
translator - before it could hold a single conversation. That made adding a product a developer's
job with a deployment attached, which is the opposite of what Pixel is for.

This module builds both from the product's definition instead. A product that exists only as a
definition and some records is served here, with the same engine, the same isolation and the same
validation as one that shipped code.

Scope is derived rather than declared: which named scopes a record falls in is found by following
the definition's own `scope.paths` from that record to its anchor entity, and comparing the
anchors it reaches with the scopes the caller was granted. Nothing in this file names an entity,
a field or a workspace of any particular product.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.db import use_connection
from app.definitions.contract import ProductDefinition
from app.definitions.vocabulary import Capability
from app.engine.lookup import PeopleMatch, PersonView, RecordView
from app.installed_products import ProductPackage
from app.services.record_store import PRIMARY, RecordStore


def client_action_type(action_key: str) -> str:
    """The name a client is sent for one of this product's actions."""
    return action_key.upper()


class DefinitionLookup:
    """One caller's read-only view of one product's records.

    Bound at construction to the space and the scopes the caller's grant allows. Nothing here can
    widen either, and a record outside them is reported exactly as a record that does not exist.
    """

    def __init__(self, definition: ProductDefinition, store: RecordStore,
                 visible_scope_ids: frozenset[str] | None) -> None:
        self._definition = definition
        self._store = store
        self._scope_ids = visible_scope_ids
        self._loaded: dict[str, tuple[RecordView, ...]] | None = None

    # --- records ---

    def _all(self) -> Mapping[str, tuple[RecordView, ...]]:
        """Every record this caller may see, read once per turn."""
        if self._loaded is None:
            stored = self._store.all()
            index = {(entity, record.id): record.fields
                     for entity, records in stored.items() for record in records}
            anchors = self._anchor_scopes()
            loaded = {entity: () for entity in self._definition.entities}
            for entity, records in stored.items():
                loaded[entity] = tuple(
                    RecordView(entity, record.id, self._title(entity, record.fields), dict(record.fields))
                    for record in records
                    if self._visible(entity, record.id, record.fields, index, anchors)
                )
            self._loaded = loaded
        return self._loaded

    def _title(self, entity_key: str, fields: Mapping[str, Any]) -> str:
        entity = self._definition.entities.get(entity_key)
        return str(fields.get(entity.title_field) or "") if entity else ""

    def _anchor_scopes(self) -> Mapping[str, frozenset[str]]:
        """Anchor record -> the scopes it belongs to, for this product and space."""
        if self._scope_ids is None:
            return {}
        with use_connection() as connection:
            rows = connection.execute(
                "select scope_id, anchor_id from record_scope_anchors where tenant_id = ? "
                "and product_id = ? and space_id = ?", self._store.key,
            ).fetchall()
        anchors: dict[str, set[str]] = {}
        for row in rows:
            anchors.setdefault(row["anchor_id"], set()).add(row["scope_id"])
        return {anchor: frozenset(scopes) for anchor, scopes in anchors.items()}

    def _visible(self, entity_key: str, record_id: str, fields: Mapping[str, Any],
                 index: Mapping[tuple[str, str], Mapping[str, Any]],
                 anchors: Mapping[str, frozenset[str]]) -> bool:
        if self._scope_ids is None or not anchors:
            # A product whose records are not divided into scopes shows them all to a caller who
            # reached the product at all; the product gate is then the only boundary there is.
            return True
        reached = self._anchors_of(entity_key, record_id, fields, index)
        return any(anchors.get(anchor, frozenset()) & self._scope_ids for anchor in reached)

    def _anchors_of(self, entity_key: str, record_id: str, fields: Mapping[str, Any],
                    index: Mapping[tuple[str, str], Mapping[str, Any]]) -> set[str]:
        """The anchor records this one reaches, by following the definition's own scope path.

        A record that reaches no anchor is visible to nobody once scopes exist: it belongs to no
        part of the product, and guessing that it belongs to all of them would widen the caller's
        view rather than narrow it.
        """
        path = self._definition.scope.paths.get(entity_key)
        if path is None:
            return set()
        if not path:
            return {record_id}
        hop = [(entity_key, record_id, fields)]
        for field_name in path:
            following: list[tuple[str, str, Mapping[str, Any]]] = []
            for entity_at_hop, _, values in hop:
                spec = self._definition.entities[entity_at_hop].fields.get(field_name)
                if spec is None or spec.target is None:
                    continue
                value = values.get(field_name)
                for target_id in ([value] if isinstance(value, str) else (value or [])):
                    target = index.get((spec.target, str(target_id))) if target_id else None
                    if target is not None:
                        following.append((spec.target, str(target_id), target))
            hop = following
        return {reached_id for _, reached_id, _ in hop}

    # --- RecordLookup ---

    def get(self, entity: str, record_id: str) -> RecordView | None:
        wanted = record_id.lower()
        return next((r for r in self._all().get(entity, ()) if r.id.lower() == wanted), None)

    def search(self, entity: str, text: str, limit: int) -> list[RecordView]:
        needle = text.lower().strip()
        if not needle:
            return []
        return [r for r in self._all().get(entity, ()) if needle in r.title.lower()][:limit]

    def by_person(self, entity: str, person_id: str, limit: int) -> list[RecordView]:
        field = self._person_field(entity)
        if field is None:
            return []
        return [r for r in self._all().get(entity, ()) if r.fields.get(field) == person_id][:limit]

    def people(self, text: str, limit: int) -> PeopleMatch:
        people = self._definition.people
        if people is None:
            return PeopleMatch()
        wanted = text.lower().split()
        if not wanted:
            return PeopleMatch()
        matches = tuple(
            PersonView(r.id, r.title) for r in self._all().get(people.entity, ())
            if r.title.lower().split() == wanted
            or (len(wanted) == 1 and wanted[0] in r.title.lower().split())
        )
        return PeopleMatch(matches[:limit])

    def count(self, entity: str) -> int:
        return len(self._all().get(entity, ()))

    def _person_field(self, entity_key: str) -> str | None:
        for reference in (self._definition.people.assigned_by if self._definition.people else ()):
            entity, _, field_name = str(reference).partition(".")
            if entity == entity_key:
                return field_name
        return None

    # --- snapshot source ---

    @property
    def scope_label(self) -> str:
        if self._scope_ids is None:
            return self._definition.identity.product_name
        with use_connection() as connection:
            rows = connection.execute(
                "select scope_id, name from record_scopes where tenant_id = ? and product_id = ? "
                "and space_id = ?", self._store.key,
            ).fetchall()
        names = {row["scope_id"]: row["name"] for row in rows}
        # Only a scope the product actually named is spoken. An identifier the platform uses
        # internally is never read out to someone: a product with no named scopes is simply
        # itself.
        labels = sorted(names[scope] for scope in self._scope_ids if scope in names)
        return ", ".join(labels) if labels else self._definition.identity.product_name

    def records_from(self, data: Mapping[str, Any]) -> Mapping[str, tuple[RecordView, ...]]:
        """This product's own records. `data` belongs to whichever product owns the legacy record
        tables, so it is not read here."""
        return self._all()

    def materialize(self, connection) -> Mapping[str, tuple[RecordView, ...]]:
        return self._all()


class DefinitionTranslator:
    """Every action the definition declares can be expressed, because the client renders from the
    same definition. A key the definition does not declare is refused rather than guessed."""

    def __init__(self, definition: ProductDefinition, lookup: Any) -> None:
        self._definition = definition
        self._lookup = lookup

    def can_translate(self, action_key: str) -> bool:
        return action_key in self._definition.actions

    def translate(self, validated: Any) -> Any:
        action = validated.action
        if action.action_key not in self._definition.actions:
            raise LookupError(action.action_key)
        payload: dict[str, Any] = {}
        spec = self._definition.actions[action.action_key]
        if spec.capability == Capability.NAVIGATE_VIEW and spec.view:
            # A request to go somewhere says where, so a client can act on it even when the
            # answer came from a different product than the one it is showing.
            payload["view"] = spec.view
        if action.target is not None:
            payload["record_id"] = action.target.id
        if action.filter is not None:
            payload[action.filter.field] = action.filter.value
        payload.update({name: value for name, value in (action.fields or {}).items()})
        payload.update({name: value for name, value in (action.prefill or {}).items()})
        return _ClientAction(client_action_type(action.action_key), payload)

    def change_set(self, validated: Any) -> dict[str, Any]:
        """The exact change an execution key binds, named by the action the definition declares."""
        action = validated.action
        spec = self._definition.actions.get(action.action_key)
        if spec is None or spec.capability not in (Capability.CREATE_RECORD, Capability.UPDATE_RECORD):
            raise LookupError(f"{action.action_key} is not a keyed mutation")
        fields = {name: value for name, value in (action.fields or {}).items()}
        if spec.capability == Capability.CREATE_RECORD:
            return {"action": action.action_key, "entity": spec.entity, "fields": fields}
        if action.target is None or not fields:
            raise LookupError("an update needs a target and at least one change")
        return {"action": action.action_key, "entity": spec.entity,
                "target": action.target.id, "changes": fields}


class _ClientAction:
    __slots__ = ("type", "payload")

    def __init__(self, type: str, payload: dict[str, Any]) -> None:
        self.type = type
        self.payload = payload


def package_from(definition: ProductDefinition, tenant_id: str, product_id: str,
                 space_id: str = PRIMARY) -> ProductPackage:
    """A package for a product that ships no code, built from its definition alone."""
    store = RecordStore(tenant_id, product_id, space_id)

    def lookup_for(grant: Any, _store: Any = None) -> DefinitionLookup:
        return DefinitionLookup(definition, store, grant.visible_scope_ids())

    return ProductPackage(
        definition_id=definition.definition.definition_id,
        lookup_factory=lookup_for,
        legacy_translator=lambda lookup: DefinitionTranslator(definition, lookup),
        client_action_types=frozenset(client_action_type(key) for key in definition.actions),
    )
