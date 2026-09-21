"""Transitional access control for the legacy demo record tables.

The legacy record tables are not yet owned by products (records migrate in Milestone 3,
step 3.5). Until then:

- the tables belong to exactly one designated product, declared by its product package's
  development seed; without a designation, nobody can reach them;
- a record grant (visible workspace scopes and record administration) belongs to one
  organization, one product and one user, never to a user across organizations.

Delete this module when records are product-owned.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from app.db import get_connection, use_connection
from app.services.demo_instances import DemoContext


@dataclass(frozen=True)
class RecordGrant:
    scope_ids: frozenset[str]
    is_admin: bool
    demo_context: DemoContext | None = None

    def visible_scope_ids(self) -> frozenset[str] | None:
        """Scopes whose records are visible; None means every scope."""
        return None if self.is_admin else self.scope_ids

    def may_use_any(self, scope_ids: set[str]) -> bool:
        return self.is_admin or bool(scope_ids & self.scope_ids)

    def may_use(self, scope_id: str) -> bool:
        return self.is_admin or scope_id in self.scope_ids


@dataclass(frozen=True)
class LegacyRecordOwner:
    tenant_id: str
    product_id: str


def designate_legacy_owner(tenant_id: str, product_id: str) -> None:
    """Record the product that owns the legacy tables. The first designation stands."""
    with get_connection() as connection:
        connection.execute(
            "insert or ignore into legacy_record_owner(singleton, tenant_id, product_id) values (1, ?, ?)",
            (tenant_id, product_id),
        )


def legacy_record_owner(connection=None) -> LegacyRecordOwner | None:
    with use_connection(connection) as connection:
        row = connection.execute("select tenant_id, product_id from legacy_record_owner").fetchone()
    return LegacyRecordOwner(row["tenant_id"], row["product_id"]) if row else None


def grant_records(tenant_id: str, product_id: str, user_id: str, scope_ids: list[str], is_admin: bool) -> None:
    """Create a grant if none exists; existing grants are left as operators set them."""
    with get_connection() as connection:
        connection.execute(
            "insert or ignore into record_grants(tenant_id, product_id, user_id, scope_ids, is_admin) "
            "values (?, ?, ?, ?, ?)",
            (tenant_id, product_id, user_id, json.dumps(sorted(scope_ids)), int(is_admin)),
        )


def record_grant(tenant_id: str, product_id: str, user_id: str, connection=None) -> RecordGrant | None:
    with use_connection(connection) as connection:
        row = connection.execute(
            "select scope_ids, is_admin from record_grants where tenant_id = ? and product_id = ? and user_id = ?",
            (tenant_id, product_id, user_id),
        ).fetchone()
    if row is None:
        return None
    return RecordGrant(scope_ids=frozenset(json.loads(row["scope_ids"])), is_admin=bool(row["is_admin"]))
