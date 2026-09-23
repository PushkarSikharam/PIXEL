"""Organizations, teams, memberships and product bindings.

Logical tenancy is Organization → Team → Product. A product binding is one product's Pixel:
it belongs to one organization, is owned by one team, and selects a published definition
version and a knowledge version. Every state change here is scoped to one organization and
affects nothing outside it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.db import get_connection, use_connection
from app.definitions.contract import TenantSettings
from app.definitions.registry import DefinitionRegistry, RegistryError
from app.definitions.safety import check_slug, check_text
from app.definitions.vocabulary import (
    MEMBER_ROLES,
    ORGANIZATION_STATES,
    PRODUCT_STATES,
    TEAM_ROLES,
    TEAM_STATES,
)


@dataclass(frozen=True)
class Organization:
    tenant_id: str
    name: str
    state: str


@dataclass(frozen=True)
class Team:
    tenant_id: str
    team_id: str
    name: str
    state: str


@dataclass(frozen=True)
class Membership:
    tenant_id: str
    user_id: str
    role: str
    team_id: str | None


@dataclass(frozen=True)
class ProductBinding:
    tenant_id: str
    product_id: str
    team_id: str
    definition_id: str
    definition_version: int
    definition_checksum: str
    knowledge_version: int
    knowledge_checksum: str | None
    state: str
    visitor_access: bool
    settings: TenantSettings


class OrganizationDirectory:
    def __init__(self, definitions: DefinitionRegistry | None = None, connection=None) -> None:
        # With a connection, reads join the caller's transaction (execution re-checks, 3.2 slice 3).
        self.definitions = definitions or DefinitionRegistry(connection=connection)
        self._connection = connection

    # --- Organizations and teams ---

    def create_organization(self, tenant_id: str, name: str) -> Organization:
        check_slug(tenant_id)
        check_text(name)
        with get_connection() as connection:
            connection.execute(
                "insert into organizations(tenant_id, name, state, created_at) values (?, ?, 'active', ?)",
                (tenant_id, name, _now()),
            )
        return self.organization(tenant_id)

    def organization(self, tenant_id: str) -> Organization | None:
        with use_connection(self._connection) as connection:
            row = connection.execute("select * from organizations where tenant_id = ?", (tenant_id,)).fetchone()
        return Organization(row["tenant_id"], row["name"], row["state"]) if row else None

    def set_organization_state(self, tenant_id: str, state: str) -> Organization:
        self._require(state, ORGANIZATION_STATES, "organization state")
        self._update("organizations", "state = ?", (state,), "tenant_id = ?", (tenant_id,))
        return self.organization(tenant_id)

    def create_team(self, tenant_id: str, team_id: str, name: str) -> Team:
        check_slug(team_id)
        check_text(name)
        if self.organization(tenant_id) is None:
            raise RegistryError(f"organization {tenant_id} does not exist")
        with get_connection() as connection:
            connection.execute(
                "insert into teams(tenant_id, team_id, name, state, created_at) values (?, ?, ?, 'active', ?)",
                (tenant_id, team_id, name, _now()),
            )
        return self.team(tenant_id, team_id)

    def team(self, tenant_id: str, team_id: str) -> Team | None:
        with use_connection(self._connection) as connection:
            row = connection.execute(
                "select * from teams where tenant_id = ? and team_id = ?", (tenant_id, team_id)
            ).fetchone()
        return Team(row["tenant_id"], row["team_id"], row["name"], row["state"]) if row else None

    def set_team_state(self, tenant_id: str, team_id: str, state: str) -> Team:
        self._require(state, TEAM_STATES, "team state")
        self._update("teams", "state = ?", (state,), "tenant_id = ? and team_id = ?", (tenant_id, team_id))
        return self.team(tenant_id, team_id)

    # --- Organization users ---

    def add_member(self, tenant_id: str, user_id: str, role: str, team_id: str | None = None) -> Membership:
        self._require(role, MEMBER_ROLES, "role")
        if (role in TEAM_ROLES) != (team_id is not None):
            raise RegistryError("team roles, and only team roles, belong to a team")
        if team_id is not None and self.team(tenant_id, team_id) is None:
            raise RegistryError(f"team {team_id} does not exist in {tenant_id}")
        with get_connection() as connection:
            connection.execute(
                "insert into memberships(tenant_id, user_id, role, team_id) values (?, ?, ?, ?)",
                (tenant_id, user_id, role, team_id),
            )
        return self.membership(tenant_id, user_id)

    def set_member_role(self, tenant_id: str, user_id: str, role: str, team_id: str | None = None) -> Membership:
        self._require(role, MEMBER_ROLES, "role")
        if (role in TEAM_ROLES) != (team_id is not None):
            raise RegistryError("team roles, and only team roles, belong to a team")
        if team_id is not None and self.team(tenant_id, team_id) is None:
            raise RegistryError(f"team {team_id} does not exist in {tenant_id}")
        self._update("memberships", "role = ?, team_id = ?", (role, team_id),
                     "tenant_id = ? and user_id = ?", (tenant_id, user_id))
        return self.membership(tenant_id, user_id)

    def membership(self, tenant_id: str, user_id: str) -> Membership | None:
        with use_connection(self._connection) as connection:
            row = connection.execute(
                "select * from memberships where tenant_id = ? and user_id = ?", (tenant_id, user_id)
            ).fetchone()
        return Membership(row["tenant_id"], row["user_id"], row["role"], row["team_id"]) if row else None

    def organizations_of(self, user_id: str) -> list[str]:
        with use_connection(self._connection) as connection:
            rows = connection.execute(
                "select tenant_id from memberships where user_id = ? order by tenant_id", (user_id,)
            ).fetchall()
        return [row["tenant_id"] for row in rows]

    # --- Products ---

    def bind_product(
        self,
        tenant_id: str,
        product_id: str,
        team_id: str,
        definition_id: str,
        definition_version: int,
        knowledge_version: int = 1,
        knowledge_checksum: str | None = None,
        visitor_access: bool = False,
        settings: dict | None = None,
    ) -> ProductBinding:
        """Create one product's Pixel for a team, on a published definition the organization may use."""
        check_slug(product_id)
        if self.team(tenant_id, team_id) is None:
            raise RegistryError(f"team {team_id} does not exist in {tenant_id}")
        version = self._bindable(tenant_id, definition_id, definition_version)
        validated_settings = TenantSettings.model_validate(settings or {})
        with get_connection() as connection:
            connection.execute("begin immediate")
            if connection.execute(
                "select 1 from product_bindings where tenant_id = ? and product_id = ?", (tenant_id, product_id)
            ).fetchone():
                raise RegistryError(f"{tenant_id} already has a product {product_id}")
            connection.execute(
                """
                insert into product_bindings(
                  tenant_id, product_id, team_id, definition_id, definition_version, definition_checksum,
                  knowledge_version, knowledge_checksum, state, visitor_access, settings_json, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    tenant_id, product_id, team_id, definition_id, definition_version, version.checksum,
                    knowledge_version, knowledge_checksum, int(visitor_access),
                    validated_settings.model_dump_json(exclude_none=True), _now(),
                ),
            )
        return self.product(tenant_id, product_id)

    def product(self, tenant_id: str, product_id: str) -> ProductBinding | None:
        with use_connection(self._connection) as connection:
            row = connection.execute(
                "select * from product_bindings where tenant_id = ? and product_id = ?", (tenant_id, product_id)
            ).fetchone()
        if row is None:
            return None
        return self._binding(row)

    def active_products(self) -> list[ProductBinding]:
        """Every active product binding in this deployment, across organizations.

        Used by platform health, never by a request: nothing tenant-facing may list another
        organization's products.
        """
        with use_connection(self._connection) as connection:
            rows = connection.execute(
                "select * from product_bindings where state = 'active' order by tenant_id, product_id"
            ).fetchall()
        return [self._binding(row) for row in rows]

    @staticmethod
    def _binding(row) -> ProductBinding:
        return ProductBinding(
            tenant_id=row["tenant_id"],
            product_id=row["product_id"],
            team_id=row["team_id"],
            definition_id=row["definition_id"],
            definition_version=row["definition_version"],
            definition_checksum=row["definition_checksum"],
            knowledge_version=row["knowledge_version"],
            knowledge_checksum=row["knowledge_checksum"],
            state=row["state"],
            visitor_access=bool(row["visitor_access"]),
            settings=TenantSettings.model_validate(json.loads(row["settings_json"])),
        )

    def move_product_version(self, tenant_id: str, product_id: str, definition_version: int) -> ProductBinding:
        """Upgrade or roll back one product. Only new sessions use the new version."""
        binding = self._existing(tenant_id, product_id)
        version = self._bindable(tenant_id, binding.definition_id, definition_version)
        self._update_product(
            tenant_id, product_id, "definition_version = ?, definition_checksum = ?",
            (definition_version, version.checksum),
        )
        return self.product(tenant_id, product_id)

    def set_product_state(self, tenant_id: str, product_id: str, state: str) -> ProductBinding:
        self._require(state, PRODUCT_STATES, "product state")
        self._update_product(tenant_id, product_id, "state = ?", (state,))
        return self.product(tenant_id, product_id)

    def transfer_product(self, tenant_id: str, product_id: str, team_id: str) -> ProductBinding:
        """Give a product to another team of the same organization. Live sessions end."""
        if self.team(tenant_id, team_id) is None:
            raise RegistryError(f"team {team_id} does not exist in {tenant_id}")
        self._update_product(tenant_id, product_id, "team_id = ?", (team_id,))
        return self.product(tenant_id, product_id)

    def _bindable(self, tenant_id: str, definition_id: str, definition_version: int):
        version = self.definitions.get(definition_id, definition_version)
        if version is None or version.state != "published":
            raise RegistryError(f"{definition_id} v{definition_version} is not published")
        if not version.bindable_by(tenant_id):
            raise RegistryError(f"{definition_id} is private to another organization")
        return version

    def _existing(self, tenant_id: str, product_id: str) -> ProductBinding:
        binding = self.product(tenant_id, product_id)
        if binding is None:
            raise RegistryError(f"{tenant_id} has no product {product_id}")
        return binding

    def _update_product(self, tenant_id: str, product_id: str, assignments: str, values: tuple) -> None:
        self._update(
            "product_bindings", f"{assignments}, updated_at = ?", (*values, _now()),
            "tenant_id = ? and product_id = ?", (tenant_id, product_id),
        )

    @staticmethod
    def _update(table: str, assignments: str, values: tuple, where: str, keys: tuple) -> None:
        with get_connection() as connection:
            cursor = connection.execute(f"update {table} set {assignments} where {where}", (*values, *keys))
        if cursor.rowcount != 1:
            raise RegistryError(f"no matching row in {table}")

    @staticmethod
    def _require(value: str, allowed: tuple[str, ...], label: str) -> None:
        if value not in allowed:
            raise RegistryError(f"unknown {label} {value}")


def _now() -> str:
    return datetime.now(UTC).isoformat()
