"""Private, disposable record storage for public product-demo visitors.

All changes use the existing database; provider accounting is never copied or reset. The
caller supplies a server-resolved visitor and an installed seed, never browser-selected owners.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from uuid import uuid4

from app.db import get_connection

logger = logging.getLogger("pixel.demo")


class InstanceUnavailable(ValueError):
    pass


class InstanceConflict(ValueError):
    pass


class InstanceCapacity(ValueError):
    pass


class SeedNotApproved(ValueError):
    """The installed seed differs from the version and checksum the product package approved."""


@dataclass(frozen=True)
class DemoContext:
    tenant_id: str
    product_id: str
    visitor_id: str
    instance_id: str
    generation: int


@dataclass(frozen=True)
class DemoSeed:
    version: str
    # Canonical JSON is an immutable snapshot, not a reference to mutable seed objects.
    content: str
    checksum: str

    @classmethod
    def from_records(cls, version: str, records: dict[str, dict[str, dict]]) -> DemoSeed:
        if not version or not isinstance(records, dict):
            raise ValueError("seed version and records are required")
        for entity, values in records.items():
            if not isinstance(entity, str) or not entity or not isinstance(values, dict):
                raise ValueError("seed entities must be named record maps")
            for record_id, value in values.items():
                if not isinstance(record_id, str) or not record_id or not isinstance(value, dict):
                    raise ValueError("seed records must have identifiers and object values")
        content = json.dumps(records, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return cls(version, content, hashlib.sha256(content.encode()).hexdigest())

    def records(self) -> dict[str, dict[str, dict]]:
        if hashlib.sha256(self.content.encode()).hexdigest() != self.checksum:
            raise ValueError("seed checksum mismatch")
        return json.loads(self.content)


@dataclass(frozen=True)
class InstanceLimits:
    # Each instance is a few dozen small rows, so capacity is cheap; it exists to bound abuse.
    active_per_product: int = 1000
    records_per_instance: int = 1000
    absolute_seconds: int = 86400
    idle_seconds: int = 7200
    # When every slot is taken, the instance unused for longest may be reclaimed for a new
    # visitor, but only after this long without use. An instance in use is never evicted, so
    # holding every slot means keeping every instance busy, not just allocating once.
    reclaim_after_seconds: int = 900

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in (
            self.active_per_product, self.records_per_instance,
            self.absolute_seconds, self.idle_seconds, self.reclaim_after_seconds,
        )):
            raise ValueError("instance limits must be positive integers")
        if self.reclaim_after_seconds > self.idle_seconds:
            raise ValueError("an instance must be reclaimable before it expires on its own")


def create_instance_schema(connection) -> None:
    """Additive schema only. Existing member records and usage tables are left intact."""
    connection.executescript("""
        create table if not exists demo_instances(
            tenant_id text not null,
            product_id text not null,
            instance_id text not null,
            visitor_id text not null,
            generation integer not null check (generation > 0),
            seed_version text not null,
            seed_checksum text not null,
            created_at real not null,
            expires_at real not null,
            idle_expires_at real not null,
            last_active_at real,
            primary key (tenant_id, product_id, instance_id),
            unique (tenant_id, product_id, visitor_id)
        );
        create table if not exists demo_instance_records(
            tenant_id text not null,
            product_id text not null,
            instance_id text not null,
            entity text not null,
            record_id text not null,
            revision integer not null check (revision > 0),
            payload text not null check (json_valid(payload)),
            primary key (tenant_id, product_id, instance_id, entity, record_id),
            foreign key (tenant_id, product_id, instance_id)
                references demo_instances(tenant_id, product_id, instance_id) on delete cascade
        );
        create table if not exists demo_instance_receipts(
            tenant_id text not null,
            product_id text not null,
            instance_id text not null,
            generation integer not null,
            request_key text not null,
            request_digest text not null,
            entity text not null,
            record_id text not null,
            primary key (tenant_id, product_id, instance_id, generation, request_key),
            foreign key (tenant_id, product_id, instance_id)
                references demo_instances(tenant_id, product_id, instance_id) on delete cascade
        );
        create index if not exists demo_instances_expiry
            on demo_instances(expires_at, idle_expires_at);
    """)
    # Added after the first release: instances created before it count as last used at creation.
    columns = {row["name"] for row in connection.execute("pragma table_info(demo_instances)")}
    if "last_active_at" not in columns:
        connection.execute("alter table demo_instances add column last_active_at real")
        connection.execute("update demo_instances set last_active_at = created_at")
    connection.execute(
        "create index if not exists demo_instances_activity "
        "on demo_instances(tenant_id, product_id, last_active_at)"
    )


class DemoInstanceStore:
    def __init__(self, limits: InstanceLimits | None = None, clock=time.time):
        self.limits = limits or InstanceLimits(
            active_per_product=int(os.environ.get("PIXEL_DEMO_INSTANCE_ACTIVE_LIMIT", "1000")),
            records_per_instance=int(os.environ.get("PIXEL_DEMO_INSTANCE_RECORD_LIMIT", "1000")),
            absolute_seconds=int(os.environ.get("PIXEL_DEMO_INSTANCE_TTL_SECONDS", "86400")),
            idle_seconds=int(os.environ.get("PIXEL_DEMO_INSTANCE_IDLE_SECONDS", "7200")),
            reclaim_after_seconds=int(os.environ.get("PIXEL_DEMO_INSTANCE_RECLAIM_SECONDS", "900")),
        )
        self.clock = clock

    def allocate(self, tenant_id: str, product_id: str, visitor_id: str, seed: DemoSeed,
                 connection=None) -> DemoContext:
        """Storage primitive: authorization and token issuance belong to the calling service."""
        if not all(isinstance(value, str) and value for value in (tenant_id, product_id, visitor_id)):
            raise ValueError("server-resolved ownership is required")
        records = seed.records()
        self._check_size(records)
        context = DemoContext(tenant_id, product_id, visitor_id, str(uuid4()), 1)
        with self._write_transaction(connection) as connection:
            now = self.clock()
            self._prune_expired(connection, now, limit=100)
            active = connection.execute(
                "select count(*) from demo_instances where tenant_id=? and product_id=? "
                "and expires_at>? and idle_expires_at>?", (tenant_id, product_id, now, now),
            ).fetchone()[0]
            if active >= self.limits.active_per_product and not self._reclaim_unused(
                connection, tenant_id, product_id, now,
            ):
                logger.warning(json.dumps({
                    "event": "demo_capacity_reached", "tenant_id": tenant_id,
                    "product_id": product_id, "active": active,
                }))
                raise InstanceCapacity("Demo capacity reached.")
            connection.execute(
                "insert into demo_instances(tenant_id, product_id, instance_id, visitor_id, "
                "generation, seed_version, seed_checksum, created_at, expires_at, "
                "idle_expires_at, last_active_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*self.key(context), visitor_id, 1, seed.version, seed.checksum, now,
                 now + self.limits.absolute_seconds, now + self.limits.idle_seconds, now),
            )
            self._insert_seed(connection, context, records)
        return context

    def read(self, context: DemoContext, connection=None) -> dict[str, dict[str, dict]]:
        # Materialize one consistent snapshot, then release the transaction before any model call.
        if connection is None:
            with get_connection() as own:
                own.execute("begin")
                self._check(own, context)
                rows = self._record_rows(own, context)
        else:
            self._check(connection, context)
            rows = self._record_rows(connection, context)
        result: dict[str, dict[str, dict]] = {}
        for row in rows:
            result.setdefault(row["entity"], {})[row["record_id"]] = {
                "revision": row["revision"], "data": json.loads(row["payload"]),
            }
        return result

    def _record_rows(self, connection, context: DemoContext):
        return connection.execute(
            "select entity, record_id, revision, payload from demo_instance_records "
            "where tenant_id=? and product_id=? and instance_id=? order by entity, record_id",
            self.key(context),
        ).fetchall()

    def touch(self, context: DemoContext) -> None:
        with get_connection() as connection:
            connection.execute("begin immediate")
            self._check(connection, context)
            now = self.clock()
            connection.execute(
                "update demo_instances set idle_expires_at=min(expires_at, ?), last_active_at=? "
                "where tenant_id=? and product_id=? and instance_id=?",
                (now + self.limits.idle_seconds, now, *self.key(context)),
            )

    def put(self, context: DemoContext, entity: str, record_id: str, payload: dict,
            *, expected_revision: int | None, references: tuple[tuple[str, str], ...] = (),
            connection=None) -> int:
        """Create with expected_revision=None; replace only the exact revision the caller read.

        Product adapters must derive references from validated fields, not a client-supplied list.
        This repository verifies those links exist in the same instance inside the write transaction.
        A supplied connection must already hold the caller's write transaction, so an execution
        receipt and record change can settle atomically without a second database connection.
        """
        if not entity or not record_id or not isinstance(payload, dict):
            raise ValueError("record entity, identifier and object payload are required")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
            raise ValueError("expected revision must be a positive integer")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode()) > 65536:
            raise InstanceCapacity("Demo record is too large.")
        key = (*self.key(context), entity, record_id)
        with self._write_transaction(connection) as connection:
            self._check(connection, context)
            row = connection.execute(
                "select revision from demo_instance_records where tenant_id=? and product_id=? "
                "and instance_id=? and entity=? and record_id=?", key,
            ).fetchone()
            if (row[0] if row else None) != expected_revision:
                raise InstanceConflict("Record changed or is unavailable; reload before saving.")
            for target_entity, target_id in references:
                exists = connection.execute(
                    "select 1 from demo_instance_records where tenant_id=? and product_id=? "
                    "and instance_id=? and entity=? and record_id=?",
                    (*self.key(context), target_entity, target_id),
                ).fetchone()
                if exists is None:
                    raise InstanceUnavailable("Referenced record is unavailable.")
            if row is None:
                count = connection.execute(
                    "select count(*) from demo_instance_records where tenant_id=? and product_id=? "
                    "and instance_id=?", self.key(context),
                ).fetchone()[0]
                if count >= self.limits.records_per_instance:
                    raise InstanceCapacity("Demo record limit reached.")
            revision = (expected_revision or 0) + 1
            connection.execute(
                "insert into demo_instance_records values (?, ?, ?, ?, ?, ?, ?) "
                "on conflict(tenant_id, product_id, instance_id, entity, record_id) "
                "do update set revision=excluded.revision, payload=excluded.payload",
                (*key, revision, encoded),
            )
        return revision

    def reset(self, context: DemoContext, seed: DemoSeed, connection=None) -> DemoContext:
        records = seed.records()
        self._check_size(records)
        with self._write_transaction(connection) as connection:
            row = self._check(connection, context)
            if (row["seed_version"], row["seed_checksum"]) != (seed.version, seed.checksum):
                raise InstanceConflict("The pinned demo seed is unavailable.")
            connection.execute(
                "delete from demo_instance_records where tenant_id=? and product_id=? and instance_id=?",
                self.key(context),
            )
            # Form idempotency receipts belong to one disposable generation. The old token is
            # invalid after reset, so retaining them would only create unbounded metadata growth.
            connection.execute(
                "delete from demo_instance_receipts "
                "where tenant_id=? and product_id=? and instance_id=?",
                self.key(context),
            )
            connection.execute(
                "update demo_instances set generation=generation+1, "
                "idle_expires_at=min(expires_at, ?) where tenant_id=? "
                "and product_id=? and instance_id=?",
                (self.clock() + self.limits.idle_seconds, *self.key(context)),
            )
            updated = replace(context, generation=context.generation + 1)
            self._insert_seed(connection, updated, records)
        return updated

    @contextmanager
    def transaction(self, context: DemoContext):
        """One private-instance write transaction for a product adapter or reset workflow."""
        with get_connection() as connection:
            connection.execute("begin immediate")
            self._check(connection, context)
            yield connection

    def assert_available(self, context: DemoContext, connection=None) -> None:
        if connection is None:
            with get_connection() as own:
                self._check(own, context)
            return
        self._check(connection, context)

    def prune(self, *, limit: int = 100) -> int:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("cleanup batch must be between 1 and 1000")
        with get_connection() as connection:
            connection.execute("begin immediate")
            now = self.clock()
            return self._prune_expired(connection, now, limit)

    def _reclaim_unused(self, connection, tenant_id: str, product_id: str, now: float) -> bool:
        """At capacity, free the instance unused for longest, if it has been unused long enough.

        Its visitor finds their demo gone on their next request and starts a fresh one. A visitor
        who used their demo within the reclaim window keeps it.
        """
        row = connection.execute(
            "select instance_id, last_active_at from demo_instances "
            "where tenant_id=? and product_id=? and expires_at>? and idle_expires_at>? "
            "and coalesce(last_active_at, created_at)<=? "
            "order by coalesce(last_active_at, created_at) limit 1",
            (tenant_id, product_id, now, now, now - self.limits.reclaim_after_seconds),
        ).fetchone()
        if row is None:
            return False
        connection.execute(
            "delete from demo_instances where tenant_id=? and product_id=? and instance_id=?",
            (tenant_id, product_id, row["instance_id"]),
        )
        logger.info(json.dumps({
            "event": "demo_instance_reclaimed", "tenant_id": tenant_id, "product_id": product_id,
            "unused_seconds": round(now - (row["last_active_at"] or now)),
        }))
        return True

    @staticmethod
    def _prune_expired(connection, now: float, limit: int) -> int:
        rows = connection.execute(
            "select tenant_id, product_id, instance_id from demo_instances "
            "where expires_at<=? or idle_expires_at<=? order by expires_at limit ?",
            (now, now, limit),
        ).fetchall()
        for row in rows:
            connection.execute(
                "delete from demo_instances where tenant_id=? and product_id=? and instance_id=?",
                tuple(row),
            )
        return len(rows)

    @staticmethod
    def key(context: DemoContext) -> tuple[str, str, str]:
        return context.tenant_id, context.product_id, context.instance_id

    @staticmethod
    @contextmanager
    def _write_transaction(connection):
        if connection is not None:
            if not connection.in_transaction:
                raise RuntimeError("caller-owned connection requires a write transaction")
            yield connection
            return
        with get_connection() as own:
            own.execute("begin immediate")
            yield own

    def _check(self, connection, context: DemoContext):
        row = connection.execute(
            "select * from demo_instances where tenant_id=? and product_id=? and instance_id=? "
            "and visitor_id=? and generation=? and expires_at>? and idle_expires_at>?",
            (*self.key(context), context.visitor_id, context.generation, self.clock(), self.clock()),
        ).fetchone()
        if row is None:
            raise InstanceUnavailable("Demo instance is unavailable.")
        return row

    def _check_size(self, records):
        if sum(len(rows) for rows in records.values()) > self.limits.records_per_instance:
            raise InstanceCapacity("Seed exceeds the demo record limit.")

    def _insert_seed(self, connection, context, records):
        for entity, values in records.items():
            for record_id, payload in values.items():
                encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
                if len(encoded.encode()) > 65536:
                    raise InstanceCapacity("Seed record is too large.")
                connection.execute(
                    "insert into demo_instance_records values (?, ?, ?, ?, ?, 1, ?)",
                    (*self.key(context), entity, record_id, encoded),
                )
