from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db
from app.services.demo_instances import (
    DemoInstanceStore, DemoSeed, InstanceCapacity, InstanceConflict,
    InstanceLimits, InstanceUnavailable, create_instance_schema,
)


class PrivateInstanceStorageTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        override = patch.object(db, "DB_PATH", Path(temporary.name) / "instances.sqlite3")
        override.start()
        self.addCleanup(override.stop)
        with db.get_connection() as connection:
            create_instance_schema(connection)
            connection.execute("create table provider_usage(marker text)")
            connection.execute("insert into provider_usage values ('must-survive')")
        self.now = 1000.0
        self.store = DemoInstanceStore(clock=lambda: self.now)
        self.seed = DemoSeed.from_records("seed-1", {
            "members": {"maya": {"name": "Maya"}, "noah": {"name": "Noah"}},
            "issues": {"LIN-142": {"assignee": "maya", "title": "Webhook retry"}},
        })
        self.a = self.store.allocate("tenant-a", "product-a", "visitor-a", self.seed)
        self.b = self.store.allocate("tenant-a", "product-a", "visitor-b", self.seed)

    def test_maya_change_is_private_and_survives_a_new_store(self):
        self.store.put(self.a, "issues", "LIN-142", {"assignee": "noah"},
                       expected_revision=1, references=(("members", "noah"),))
        restored = DemoInstanceStore(clock=lambda: self.now)
        self.assertEqual(restored.read(self.a)["issues"]["LIN-142"]["data"]["assignee"], "noah")
        self.assertEqual(restored.read(self.b)["issues"]["LIN-142"]["data"]["assignee"], "maya")

    def test_each_boundary_and_unknown_instance_give_identical_refusal(self):
        for context in (
            replace(self.a, visitor_id=self.b.visitor_id),
            replace(self.a, instance_id=self.b.instance_id),
            replace(self.a, tenant_id="tenant-b"), replace(self.a, product_id="product-b"),
            replace(self.a, generation=2), replace(self.a, instance_id="missing"),
        ):
            with self.subTest(context=context):
                for action in (lambda: self.store.read(context),
                               lambda: self.store.reset(context, self.seed),
                               lambda: self.store.put(context, "issues", "x", {}, expected_revision=None)):
                    with self.assertRaisesRegex(InstanceUnavailable, "Demo instance is unavailable"):
                        action()

    def test_reset_only_own_data_and_invalidates_old_generation(self):
        self.store.put(self.a, "issues", "LIN-142", {"assignee": "noah"}, expected_revision=1)
        self.store.put(self.b, "issues", "LIN-142", {"assignee": "noah"}, expected_revision=1)
        with db.get_connection() as connection:
            connection.execute(
                "insert into demo_instance_receipts values (?, ?, ?, ?, ?, ?, ?, ?)",
                (*self.store.key(self.a), self.a.generation, "old-key", "digest", "issues", "LIN-142"),
            )
        new = self.store.reset(self.a, self.seed)
        self.assertEqual(new.generation, 2)
        self.assertEqual(self.store.read(new)["issues"]["LIN-142"]["data"]["assignee"], "maya")
        self.assertEqual(self.store.read(self.b)["issues"]["LIN-142"]["data"]["assignee"], "noah")
        with self.assertRaises(InstanceUnavailable):
            self.store.put(self.a, "issues", "LIN-142", {}, expected_revision=1)
        with db.get_connection() as connection:
            self.assertEqual(connection.execute("select marker from provider_usage").fetchone()[0], "must-survive")
            self.assertEqual(
                connection.execute(
                    "select count(*) from demo_instance_receipts where instance_id=?",
                    (self.a.instance_id,),
                ).fetchone()[0],
                0,
            )

    def test_foreign_reference_rejects_without_partial_write(self):
        self.store.put(self.b, "members", "only-b", {"name": "Private"}, expected_revision=None)
        with self.assertRaises(InstanceUnavailable):
            self.store.put(self.a, "issues", "new", {"assignee": "only-b"},
                           expected_revision=None, references=(("members", "only-b"),))
        self.assertNotIn("new", self.store.read(self.a)["issues"])

    def test_duplicate_and_stale_revision_are_rejected(self):
        with self.assertRaises(InstanceConflict):
            self.store.put(self.a, "issues", "LIN-142", {}, expected_revision=None)
        self.store.put(self.a, "issues", "LIN-142", {}, expected_revision=1)
        with self.assertRaises(InstanceConflict):
            self.store.put(self.a, "issues", "LIN-142", {}, expected_revision=1)
        with self.assertRaises(ValueError):
            self.store.put(self.a, "issues", "LIN-142", {}, expected_revision=True)

    def test_simultaneous_edits_commit_once(self):
        def update(_):
            try:
                return self.store.put(self.a, "issues", "LIN-142", {}, expected_revision=1)
            except InstanceConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(update, range(4)))
        self.assertEqual(outcomes.count(2), 1)
        self.assertEqual(outcomes.count("conflict"), 3)

    def test_allocation_at_capacity_is_atomic(self):
        store = DemoInstanceStore(InstanceLimits(active_per_product=3), clock=lambda: self.now)
        def allocate(index):
            try:
                return store.allocate("tenant-a", "product-a", f"new-{index}", self.seed)
            except InstanceCapacity:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(allocate, range(4)))
        self.assertEqual(sum(value is not None for value in outcomes), 1)

    def test_failed_seed_rolls_back_instance_and_records(self):
        original = self.store._insert_seed
        def failing(connection, context, records):
            original(connection, context, records)
            raise RuntimeError("injected failure")
        with patch.object(self.store, "_insert_seed", failing), self.assertRaises(RuntimeError):
            self.store.allocate("tenant-a", "product-a", "failed", self.seed)
        with db.get_connection() as connection:
            self.assertEqual(connection.execute("select count(*) from demo_instances").fetchone()[0], 2)
            self.assertEqual(connection.execute("select count(*) from demo_instance_records").fetchone()[0], 6)

    def test_failed_reset_preserves_previous_generation_and_data(self):
        with patch.object(self.store, "_insert_seed", side_effect=RuntimeError("failure")):
            with self.assertRaises(RuntimeError):
                self.store.reset(self.a, self.seed)
        self.assertEqual(self.store.read(self.a)["issues"]["LIN-142"]["revision"], 1)

    def test_expiry_is_fail_closed_and_cleanup_is_bounded(self):
        self.now += 7200
        with self.assertRaises(InstanceUnavailable):
            self.store.read(self.a)
        self.assertEqual(self.store.prune(limit=1), 1)
        self.assertEqual(self.store.prune(limit=1), 1)
        self.assertEqual(self.store.prune(limit=1), 0)
        with db.get_connection() as connection:
            self.assertEqual(connection.execute("select count(*) from demo_instance_records").fetchone()[0], 0)
            self.assertEqual(connection.execute("select count(*) from provider_usage").fetchone()[0], 1)

    def test_touch_never_extends_absolute_lifetime(self):
        for _ in range(12):
            self.now += 7100
            self.store.touch(self.a)
        self.now = 1000 + 86400
        with self.assertRaises(InstanceUnavailable):
            self.store.touch(self.a)

    def test_seed_is_a_snapshot_and_checksum_is_checked(self):
        data = {"issues": {"one": {"status": "Open"}}}
        seed = DemoSeed.from_records("v1", data)
        data["issues"]["one"]["status"] = "Closed"
        self.assertEqual(seed.records()["issues"]["one"]["status"], "Open")
        with self.assertRaises(ValueError):
            self.store.allocate("t", "p", "v", replace(seed, content="{}"))
        with self.assertRaises(InstanceConflict):
            self.store.reset(self.a, seed)

    def test_records_and_payload_quotas_apply(self):
        limited = DemoInstanceStore(InstanceLimits(records_per_instance=3), clock=lambda: self.now)
        with self.assertRaises(InstanceCapacity):
            limited.put(self.a, "issues", "new", {}, expected_revision=None)
        with self.assertRaises(InstanceCapacity):
            self.store.put(self.a, "issues", "new", {"text": "x" * 65536}, expected_revision=None)

    def test_caller_transaction_rolls_back_record_and_receipt_together(self):
        with self.assertRaises(RuntimeError):
            with db.get_connection() as connection:
                connection.execute("begin immediate")
                self.store.put(self.a, "issues", "LIN-142", {"assignee": "noah"},
                               expected_revision=1, connection=connection)
                connection.execute("insert into provider_usage values ('fake-receipt')")
                raise RuntimeError("before commit")
        self.assertEqual(self.store.read(self.a)["issues"]["LIN-142"]["data"]["assignee"], "maya")
        with db.get_connection() as connection:
            self.assertEqual(connection.execute("select count(*) from provider_usage").fetchone()[0], 1)
            with self.assertRaises(RuntimeError):
                self.store.put(self.a, "issues", "LIN-142", {},
                               expected_revision=1, connection=connection)

    def test_reset_racing_with_old_write_cannot_change_new_generation(self):
        def write():
            try:
                self.store.put(self.a, "issues", "LIN-142", {"assignee": "noah"}, expected_revision=1)
                return "written"
            except InstanceUnavailable:
                return "expired-generation"
        with ThreadPoolExecutor(max_workers=2) as pool:
            mutation = pool.submit(write)
            reset = pool.submit(self.store.reset, self.a, self.seed)
            self.assertIn(mutation.result(), ("written", "expired-generation"))
            updated = reset.result()
        self.assertEqual(self.store.read(updated)["issues"]["LIN-142"]["data"]["assignee"], "maya")

    def test_schema_is_additive_and_repeatable(self):
        with db.get_connection() as connection:
            create_instance_schema(connection)
            create_instance_schema(connection)
        self.assertEqual(len(self.store.read(self.a)["issues"]), 1)

    def test_tenants_and_products_have_independent_capacity_and_records(self):
        limited = DemoInstanceStore(InstanceLimits(active_per_product=1), clock=lambda: self.now)
        for tenant, product in (("tenant-b", "product-a"), ("tenant-a", "product-b")):
            context = limited.allocate(tenant, product, "visitor-a", self.seed)
            limited.put(context, "issues", "LIN-142", {"assignee": "noah"}, expected_revision=1)
        self.assertEqual(self.store.read(self.a)["issues"]["LIN-142"]["data"]["assignee"], "maya")


if __name__ == "__main__":
    unittest.main()
