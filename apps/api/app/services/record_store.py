"""Records of any product, shaped by the definition that declares them (3.5).

Nothing here knows what a product's entities are called. A record is an entity name, an
identifier and a payload; what fields that payload may carry, which of them are required, what an
enum may contain, which records a reference may point at and how an identifier is minted are all
read from the product's own definition.

A **space** says whose records these are. `PRIMARY` is an organization's own, lasting records -
what someone who signs in to Pixel keeps. A demo instance id is one visitor's private, expiring
copy. Both go through this store, so a visitor's isolation guarantees are the same code as an
organization's, rather than written twice.

Reads are bound to the caller's scopes when the store is built and cannot be widened afterwards.
A record outside them is indistinguishable from one that does not exist.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from app.db import get_connection, use_connection
from app.definitions.contract import EntitySpec, ProductDefinition
from app.definitions.vocabulary import REFERENCE_FIELD_TYPES

PRIMARY = "primary"
MAX_PAYLOAD_BYTES = 65536
MAX_RECORDS_PER_SPACE = 10000
_SLUG = re.compile(r"[^a-z0-9]+")


class RecordInvalid(ValueError):
    """The payload does not satisfy the entity its product declares."""


class RecordConflict(ValueError):
    """The record changed underneath the caller, or its reference is unavailable."""


class SpaceFull(ValueError):
    """This space holds as many records as it may."""


@dataclass(frozen=True)
class StoredRecord:
    entity: str
    id: str
    revision: int
    fields: Mapping[str, Any]


def validate(entity: EntitySpec, payload: Mapping[str, Any], *, creating: bool) -> dict[str, Any]:
    """The payload as it may be stored, or `RecordInvalid` saying what is wrong with it.

    Every rule here is the definition's own: no field name, limit or value is written in this file.
    """
    unknown = sorted(set(payload) - set(entity.fields))
    if unknown:
        raise RecordInvalid(f"unknown field: {', '.join(unknown)}")
    checked: dict[str, Any] = {}
    for name, spec in entity.fields.items():
        if name not in payload:
            if creating and spec.default is not None:
                checked[name] = spec.default
            elif creating and spec.required:
                raise RecordInvalid(f"{name} is required")
            continue
        if not creating and not spec.editable:
            raise RecordInvalid(f"{name} cannot be changed")
        checked[name] = _value(name, spec, payload[name])
    return checked


def _value(name: str, spec, value: Any) -> Any:
    if value is None:
        if spec.required:
            raise RecordInvalid(f"{name} is required")
        return None
    if spec.type == "boolean":
        if not isinstance(value, bool):
            raise RecordInvalid(f"{name} must be true or false")
        return value
    if spec.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise RecordInvalid(f"{name} must be a whole number")
        return _bounded(name, spec, value, value)
    if spec.type == "enum":
        if value not in spec.values:
            raise RecordInvalid(f"{name} must be one of: {', '.join(spec.values)}")
        return value
    if spec.type == "date":
        if not isinstance(value, str):
            raise RecordInvalid(f"{name} must be a date")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise RecordInvalid(f"{name} must be a date like 2026-01-31") from None
        return value
    if spec.type == "text_list":
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise RecordInvalid(f"{name} must be a list of text")
        return _bounded(name, spec, len(value), list(value))
    if spec.type == "refs":
        if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
            raise RecordInvalid(f"{name} must be a list of record identifiers")
        return list(value)
    if spec.type == "ref":
        if not isinstance(value, str) or not value:
            raise RecordInvalid(f"{name} must be a record identifier")
        return value
    if not isinstance(value, str):
        raise RecordInvalid(f"{name} must be text")
    return _bounded(name, spec, len(value), value)


def _bounded(name: str, spec, measured: int, value: Any) -> Any:
    if spec.min is not None and measured < spec.min:
        raise RecordInvalid(f"{name} is too small")
    if spec.max is not None and measured > spec.max:
        raise RecordInvalid(f"{name} is too large")
    return value


def references(definition: ProductDefinition, entity_key: str,
               fields: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """The (entity, record id) pairs this payload points at, read from the definition alone."""
    entity = definition.entities[entity_key]
    found: list[tuple[str, str]] = []
    for name, value in fields.items():
        spec = entity.fields.get(name)
        if spec is None or spec.type not in REFERENCE_FIELD_TYPES or value is None:
            continue
        for target_id in ([value] if spec.type == "ref" else value):
            if target_id:
                found.append((spec.target, str(target_id)))
    return tuple(found)


@dataclass(frozen=True)
class RecordStore:
    """One product's records in one space. Reads are not narrowed here: scoping is applied by the
    lookup that owns the caller's grant, so this type stays the product's whole record set."""

    tenant_id: str
    product_id: str
    space_id: str = PRIMARY

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.tenant_id, self.product_id, self.space_id)

    def all(self, connection=None) -> dict[str, list[StoredRecord]]:
        with use_connection(connection) as connection:
            rows = connection.execute(
                "select entity, record_id, revision, payload from product_records "
                "where tenant_id = ? and product_id = ? and space_id = ? order by entity, record_id",
                self.key,
            ).fetchall()
        records: dict[str, list[StoredRecord]] = {}
        for row in rows:
            records.setdefault(row["entity"], []).append(StoredRecord(
                row["entity"], row["record_id"], row["revision"], json.loads(row["payload"])))
        return records

    def get(self, entity: str, record_id: str, connection=None) -> StoredRecord | None:
        with use_connection(connection) as connection:
            row = connection.execute(
                "select entity, record_id, revision, payload from product_records where "
                "tenant_id = ? and product_id = ? and space_id = ? and entity = ? and record_id = ?",
                (*self.key, entity, record_id),
            ).fetchone()
        if row is None:
            return None
        return StoredRecord(row["entity"], row["record_id"], row["revision"], json.loads(row["payload"]))

    def create(self, definition: ProductDefinition, entity_key: str, payload: Mapping[str, Any],
               connection=None) -> StoredRecord:
        entity = definition.entities[entity_key]
        fields = validate(entity, payload, creating=True)
        with self._write(connection) as connection:
            record_id = self._mint(entity, entity_key, fields, connection)
            self._check_references(definition, entity_key, fields, connection)
            self._check_capacity(connection)
            connection.execute(
                "insert into product_records values (?, ?, ?, ?, ?, 1, ?, ?)",
                (*self.key, entity_key, record_id, _encoded(fields), _now()),
            )
        return StoredRecord(entity_key, record_id, 1, fields)

    def update(self, definition: ProductDefinition, entity_key: str, record_id: str,
               changes: Mapping[str, Any], *, expected_revision: int | None = None,
               connection=None) -> StoredRecord:
        entity = definition.entities[entity_key]
        checked = validate(entity, changes, creating=False)
        with self._write(connection) as connection:
            current = self.get(entity_key, record_id, connection)
            if current is None:
                raise RecordConflict("That record is unavailable.")
            if expected_revision is not None and current.revision != expected_revision:
                raise RecordConflict("Record changed; reload before saving.")
            fields = {**current.fields, **checked}
            self._check_references(definition, entity_key, checked, connection)
            revision = current.revision + 1
            connection.execute(
                "update product_records set revision = ?, payload = ?, updated_at = ? where "
                "tenant_id = ? and product_id = ? and space_id = ? and entity = ? and record_id = ?",
                (revision, _encoded(fields), _now(), *self.key, entity_key, record_id),
            )
        return StoredRecord(entity_key, record_id, revision, fields)

    def put_many(self, definition: ProductDefinition, records: Iterable[tuple[str, str, Mapping[str, Any]]],
                 connection=None) -> int:
        """Write records with identifiers already chosen, for seeding a space in one transaction.

        References are checked once every record is in place, so a set that refers to itself can
        be seeded. A set with a reference to nothing is rejected whole.
        """
        written = 0
        with self._write(connection) as connection:
            for entity_key, record_id, payload in records:
                fields = validate(definition.entities[entity_key], payload, creating=True)
                connection.execute(
                    "insert into product_records values (?, ?, ?, ?, ?, 1, ?, ?) "
                    "on conflict(tenant_id, product_id, space_id, entity, record_id) "
                    "do update set revision = product_records.revision + 1, "
                    "payload = excluded.payload, updated_at = excluded.updated_at",
                    (*self.key, entity_key, record_id, _encoded(fields), _now()),
                )
                written += 1
            self._check_capacity(connection)
            for entity_key, records_of in self.all(connection).items():
                for record in records_of:
                    self._check_references(definition, entity_key, record.fields, connection)
        return written

    # --- internals ---

    def _write(self, connection):
        """The caller's write transaction when there is one, so a record change and whatever
        else must settle with it commit together; otherwise one of our own."""
        return use_connection(connection) if connection is not None else _transaction()

    def _mint(self, entity: EntitySpec, entity_key: str, fields: Mapping[str, Any], connection) -> str:
        """The identifier the definition asks for. A caller never supplies one."""
        if entity.id.strategy == "slug":
            source = str(fields.get(entity.id.from_field) or "").lower()
            base = _SLUG.sub("-", source).strip("-") or entity_key
            candidate, suffix = base, 2
            while self.get(entity_key, candidate, connection) is not None:
                candidate, suffix = f"{base}-{suffix}", suffix + 1
            return candidate
        used = connection.execute(
            "select record_id from product_records where tenant_id = ? and product_id = ? "
            "and space_id = ? and entity = ?", (*self.key, entity_key),
        ).fetchall()
        highest = 0
        for row in used:
            _, _, number = str(row["record_id"]).rpartition("-")
            if number.isdigit():
                highest = max(highest, int(number))
        return f"{entity.id.prefix}-{highest + 1}"

    def _check_references(self, definition: ProductDefinition, entity_key: str,
                          fields: Mapping[str, Any], connection) -> None:
        for target_entity, target_id in references(definition, entity_key, fields):
            if self.get(target_entity, target_id, connection) is None:
                raise RecordConflict("Referenced record is unavailable.")

    def _check_capacity(self, connection) -> None:
        total = connection.execute(
            "select count(*) from product_records where tenant_id = ? and product_id = ? "
            "and space_id = ?", self.key,
        ).fetchone()[0]
        if total > MAX_RECORDS_PER_SPACE:
            raise SpaceFull("This product holds as many records as it may.")


def _encoded(fields: Mapping[str, Any]) -> str:
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > MAX_PAYLOAD_BYTES:
        raise RecordInvalid("Record is too large.")
    return encoded


def _now() -> str:
    return datetime.now(UTC).isoformat()


class _transaction:
    """One write transaction, opened immediately so concurrent writers serialise."""

    def __enter__(self):
        self._connection = get_connection()
        connection = self._connection.__enter__()
        connection.execute("begin immediate")
        return connection

    def __exit__(self, *exception):
        return self._connection.__exit__(*exception)
