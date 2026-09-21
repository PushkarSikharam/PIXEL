"""Development bootstrap: synthetic demo organizations declared by product packages.

A product package may ship `seed/demo_organization.json`, describing a synthetic organization,
team, members and product for demos and tests. Seeds load only when `PIXEL_DEMO_SEEDS=true`
is set explicitly; when it is missing, nothing synthetic is ever created. Existing rows
are left alone, so operator changes such as a version rollback survive restarts.

Demo *record* data is loaded by the application's startup instead (see `main.lifespan`), because
this module must not depend on the record store. Either way it is deliberately not created on
demand by a request: a request that is refused, or that fails authorization, must never leave
product data different from how it found it.
"""
from __future__ import annotations

import json

from pydantic import Field

from app.definitions.contract import Slug, Strict
from app.definitions.loader import DefinitionSource
from app.definitions.organizations import OrganizationDirectory
from app.definitions.registry import DefinitionRegistry
from app.record_access import designate_legacy_owner, grant_records
from app.services.env import env_bool


class _SeedOrganization(Strict):
    tenant_id: Slug
    name: str


class _SeedTeam(Strict):
    team_id: Slug
    name: str


class _SeedRecordGrant(Strict):
    scope_ids: list[str] = []
    admin: bool = False


class _SeedMember(Strict):
    user_id: str
    role: str
    team: bool = False
    # Transitional: a grant on the seeded product's legacy records (until step 3.5).
    records: _SeedRecordGrant | None = None


class _SeedProduct(Strict):
    product_id: Slug
    definition_version: int = Field(ge=1)
    knowledge_version: int = Field(default=1, ge=1)
    visitor_access: bool = False
    # Transitional: this product owns the legacy record tables (until step 3.5).
    legacy_records: bool = False


class DemoOrganizationSeed(Strict):
    organization: _SeedOrganization
    team: _SeedTeam
    members: list[_SeedMember] = []
    product: _SeedProduct


def publish_lineage(registry: DefinitionRegistry, definition_id: str, version: int):
    """Publish `version` after every earlier version that was never registered.

    Publishing in order means each version is classified against its predecessor, so a breaking
    change is refused even on a fresh database, and an earlier version is there to roll back to.
    Earlier versions already registered are left in whatever state an operator put them.
    """
    for earlier in registry.source.versions(definition_id):
        if earlier < version and registry.get(definition_id, earlier) is None:
            registry.ensure_published(definition_id, earlier)
    return registry.ensure_published(definition_id, version)


def load_demo_seeds(source: DefinitionSource | None = None) -> None:
    if not env_bool("PIXEL_DEMO_SEEDS", default=False):
        return
    directory = OrganizationDirectory(DefinitionRegistry(source) if source else DefinitionRegistry())
    source = directory.definitions.source
    for definition_id in source.packages():
        path = source.seed_path(definition_id)
        if path.is_file():
            seed = DemoOrganizationSeed.model_validate(json.loads(path.read_text(encoding="utf-8")))
            _apply(directory, definition_id, seed)


def _apply(directory: OrganizationDirectory, definition_id: str, seed: DemoOrganizationSeed) -> None:
    tenant_id, team_id = seed.organization.tenant_id, seed.team.team_id
    if directory.organization(tenant_id) is None:
        directory.create_organization(tenant_id, seed.organization.name)
    if directory.team(tenant_id, team_id) is None:
        directory.create_team(tenant_id, team_id, seed.team.name)
    for member in seed.members:
        if directory.membership(tenant_id, member.user_id) is None:
            directory.add_member(tenant_id, member.user_id, member.role, team_id if member.team else None)
    if directory.product(tenant_id, seed.product.product_id) is None:
        publish_lineage(directory.definitions, definition_id, seed.product.definition_version)
        directory.bind_product(
            tenant_id,
            seed.product.product_id,
            team_id,
            definition_id,
            seed.product.definition_version,
            knowledge_version=seed.product.knowledge_version,
            visitor_access=seed.product.visitor_access,
        )
    product_id = seed.product.product_id
    if seed.product.legacy_records:
        designate_legacy_owner(tenant_id, product_id)
    for member in seed.members:
        if member.records is not None:
            grant_records(tenant_id, product_id, member.user_id, member.records.scope_ids, member.records.admin)
