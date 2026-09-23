"""Building the new conversation engine for one turn (shared by the 5a shadow and the 5b test path).

Two steps, both pure with respect to the database:

1. `prepare_turn`: convert the records the caller's grant already loaded into immutable views,
   narrowed to the workspace the request selected. A turn never sees another workspace's records.
2. `assemble_engine`: the pinned definition, a snapshot of those views, the product's translator
   and knowledge, and a capability policy that offers only what the installed app can express.

The engine itself (`app.engine.conversation_engine`) stays pure: it is given no connection, no
ledger and no transport. Everything that reads or writes lives in the caller.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from app.definitions.organizations import OrganizationDirectory
from app.engine.actions import RecordRef
from app.engine.conversation import CapabilityPolicy
from app.engine.conversation_engine import ConversationEngine
from app.engine.knowledge import KnowledgeContext
from app.product_knowledge import ApprovedKnowledge
from app.engine.lookup import RecordView
from app.engine.snapshot import LoadedRecordSource, TurnSnapshot


@dataclass(frozen=True)
class PreparedTurn:
    """What was fixed before the engine ran: the definition ID and the immutable records."""

    definition_id: str
    records: Mapping[str, tuple[RecordView, ...]]
    scope_label: str
    package: Any
    grant: Any


def prepare_turn(
    directory: OrganizationDirectory, package_for: Callable[[str], Any], principal: Any, grant: Any,
    product_id: str, visible_data: Mapping[str, Any], scope_id: str | None,
    definition: Any = None,
) -> PreparedTurn | None:
    """Immutable views of the caller's records in the selected workspace; None if no engine applies.

    The live engine answers inside the one workspace the request selected, so the records are
    narrowed to it as well: an engine wider than the live turn would propose changes the live turn
    refuses. The product's package is chosen by the definition its binding names now.

    A product that ships no package is served from its definition instead, so adding a product to
    Pixel never requires writing code. An installed package still wins, because a product that
    brought its own record source means to use it.
    """
    if scope_id is not None:
        if not grant.may_use(scope_id):
            return None
        grant = replace(grant, scope_ids=frozenset({scope_id}), is_admin=False)
    binding = directory.product(principal.tenant_id, product_id)
    if binding is None:
        return None
    package = _package(package_for, binding, principal, product_id, grant, definition)
    if package is None or package.lookup_factory is None:
        return None
    source = package.lookup_factory(grant)
    if not isinstance(source, LoadedRecordSource):
        return None
    records = MappingProxyType({
        entity: tuple(views) for entity, views in source.records_from(visible_data).items()
    })
    return PreparedTurn(binding.definition_id, records, source.scope_label, package, grant)


def _package(package_for: Callable[[str], Any], binding: Any, principal: Any, product_id: str,
             grant: Any, definition: Any):
    """The product's installed package, or one built from its definition when it has none."""
    from app.installed_products import PackageMissing
    from app.services.generic_package import package_from
    from app.services.record_store import PRIMARY

    try:
        return package_for(binding.definition_id)
    except PackageMissing:
        if definition is None:
            return None
        # A visitor's private demo is its own space; an organization's own records are lasting.
        context = getattr(principal, "demo_context", None)
        space = getattr(context, "instance_id", None) or PRIMARY
        return package_from(definition, principal.tenant_id, product_id, space)


def assemble_engine(prepared: PreparedTurn, definition: Any, pin: Any) -> tuple[ConversationEngine, Any]:
    """The engine for one turn, and the product translator it uses (None if the product has none)."""
    people = definition.people
    snapshot = TurnSnapshot(
        records=prepared.records,
        scope_label=prepared.scope_label,
        definition_checksum=pin.definition_checksum,
        taken_at=time.time(),
        people_entity=people.entity if people else None,
        person_fields=person_fields(people.assigned_by if people else ()),
    )
    package = prepared.package
    translator = package.legacy_translator(snapshot) if package.legacy_translator else None
    can_translate = getattr(translator, "can_translate", None)
    policy = CapabilityPolicy(
        # Offer only what the installed app can express; fail closed without a translator.
        translatable=can_translate if callable(can_translate) else (lambda key: False),
        # Record visibility is enforced by the snapshot; every declared action is otherwise
        # available to a caller who passed the product gates.
        permitted=lambda key: True,
    )
    context = KnowledgeContext(
        tenant_id=pin.tenant_id, product_id=pin.product_id, definition_id=pin.definition_id,
        definition_version=pin.definition_version, definition_checksum=pin.definition_checksum,
        knowledge_version=pin.knowledge_version, scope_label=prepared.scope_label or "all",
    )
    knowledge = ApprovedKnowledge(context)
    if package.knowledge_factory is not None:
        knowledge = package.knowledge_factory(KnowledgeContext(
            tenant_id=pin.tenant_id, product_id=pin.product_id, definition_id=pin.definition_id,
            definition_version=pin.definition_version, definition_checksum=pin.definition_checksum,
            knowledge_version=pin.knowledge_version, scope_label=prepared.scope_label or "all",
        ))
    engine = ConversationEngine(
        definition, snapshot, policy, knowledge,
        definition_version=pin.definition_version,
        translate=translator.translate if translator is not None else None,
    )
    return engine, translator


def selected_record(records: Mapping[str, tuple[RecordView, ...]], record_id: str | None) -> RecordRef | None:
    """The record the visitor has open, if exactly one visible record has that ID."""
    if not record_id:
        return None
    wanted = record_id.lower()
    found = [RecordRef(entity, view.id) for entity, views in records.items() for view in views
             if view.id.lower() == wanted]
    return found[0] if len(found) == 1 else None


def person_fields(assigned_by: Any) -> dict[str, str]:
    """`entity.field` style declarations, as entity -> field."""
    fields: dict[str, str] = {}
    for reference in assigned_by:
        entity, _, field_name = str(reference).partition(".")
        if entity and field_name:
            fields.setdefault(entity, field_name)
    return fields
