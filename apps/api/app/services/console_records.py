"""What Pixel knows about itself, as records its own assistant can read.

Somebody asking "how many products do I have" is asking about the application, not about a
product inside it. The things Pixel keeps - the products an organization runs, and the people in
it - live in the platform's own tables, so they are read from there and shown to the engine as
ordinary records. Nothing is copied into a record store: a count that could drift from what the
application actually has would be worse than no count at all.

It is read-only, and bound to one organization when it is built. Nothing here can widen that:
another organization's products are not absent from a filter, they are never loaded.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.db import use_connection
from app.definitions.contract import ProductDefinition
from app.definitions.organizations import OrganizationDirectory
from app.engine.lookup import PeopleMatch, PersonView, RecordView
from app.installed_products import ProductPackage
from app.services.generic_package import DefinitionTranslator, client_action_type

# The entities Pixel's own definition declares, and what each one is read from.
PRODUCT_ENTITY = "product"
MEMBER_ENTITY = "member"


class ConsoleLookup:
    """One organization's view of its own Pixel: the products it runs and the people in it."""

    def __init__(self, definition: ProductDefinition, tenant_id: str, exclude_product: str) -> None:
        self._definition = definition
        self._tenant_id = tenant_id
        # Pixel is not one of somebody's products, so it is not counted among them.
        self._exclude = exclude_product
        self._loaded: dict[str, tuple[RecordView, ...]] | None = None

    def _all(self) -> Mapping[str, tuple[RecordView, ...]]:
        if self._loaded is not None:
            return self._loaded
        loaded: dict[str, tuple[RecordView, ...]] = {
            entity: () for entity in self._definition.entities
        }
        directory = OrganizationDirectory()
        if PRODUCT_ENTITY in self._definition.entities:
            loaded[PRODUCT_ENTITY] = tuple(self._products(directory))
        if MEMBER_ENTITY in self._definition.entities:
            products = tuple(record.id for record in loaded.get(PRODUCT_ENTITY, ()))
            loaded[MEMBER_ENTITY] = tuple(self._members(products))
        self._loaded = loaded
        return loaded

    def _products(self, directory: OrganizationDirectory) -> list[RecordView]:
        found: list[RecordView] = []
        for binding in directory.active_products():
            if binding.tenant_id != self._tenant_id or binding.product_id == self._exclude:
                continue
            try:
                definition = directory.definitions.load(
                    binding.definition_id, binding.definition_version).definition
                name = definition.identity.product_name
            except Exception:
                # A product whose definition cannot be read is still a product they have.
                name = binding.product_id
            found.append(RecordView(PRODUCT_ENTITY, binding.product_id, name, {
                "state": binding.state,
                "version": binding.definition_version,
            }))
        return found

    def _members(self, products: tuple[str, ...]) -> list[RecordView]:
        with use_connection() as connection:
            rows = connection.execute(
                "select m.user_id as user_id, m.role as role, a.email as email "
                "from memberships m left join email_accounts a on a.user_id = m.user_id "
                "where m.tenant_id = ? order by m.user_id", (self._tenant_id,),
            ).fetchall()
        return [
            RecordView(MEMBER_ENTITY, row["user_id"], row["email"] or row["user_id"],
                       {"role": row["role"], "products": products})
            for row in rows
        ]

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
        return []

    def people(self, text: str, limit: int) -> PeopleMatch:
        wanted = text.lower().split()
        if not wanted:
            return PeopleMatch()
        matches = tuple(
            PersonView(r.id, r.title) for r in self._all().get(MEMBER_ENTITY, ())
            if r.title.lower().split() == wanted
            or (len(wanted) == 1 and wanted[0] in r.title.lower().split())
        )
        return PeopleMatch(matches[:limit])

    def count(self, entity: str) -> int:
        return len(self._all().get(entity, ()))

    # --- snapshot source ---

    @property
    def scope_label(self) -> str:
        return self._definition.identity.product_name

    def records_from(self, data: Mapping[str, Any]) -> Mapping[str, tuple[RecordView, ...]]:
        """Pixel's own records. `data` belongs to a product inside it and is not read here."""
        return self._all()

    def materialize(self, connection) -> Mapping[str, tuple[RecordView, ...]]:
        return self._all()


def console_package(definition: ProductDefinition, tenant_id: str,
                    product_id: str) -> ProductPackage:
    """The package that serves Pixel's own product for one organization."""
    def lookup_for(grant: Any, _store: Any = None) -> ConsoleLookup:
        return ConsoleLookup(definition, tenant_id, product_id)

    return ProductPackage(
        definition_id=definition.definition.definition_id,
        lookup_factory=lookup_for,
        legacy_translator=lambda lookup: DefinitionTranslator(definition, lookup),
        client_action_types=frozenset(client_action_type(key) for key in definition.actions),
    )
