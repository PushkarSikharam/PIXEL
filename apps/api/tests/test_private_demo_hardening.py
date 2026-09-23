"""Four review findings on private demo instances, each reproduced and held fixed.

1. The product's record lookup defaulted to the shared member records, so the 5a shadow engine
   would have read shared data during a visitor's turn.
2. One caller could fill every demo slot within the deployment's sign-in ceiling and lock
   everyone else out for the whole idle window.
3. Lookup helpers fell back to the shared records, seed files or a built-in list of names.
4. The demo seed was rebuilt from files on disk with no approved checksum.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app import db
from app.auth import product_record_grant, visitor_from_token
from app.record_access import RecordGrant
from app.services import demo_data
from app.services.demo_instances import (
    DemoInstanceStore,
    DemoSeed,
    InstanceCapacity,
    InstanceLimits,
    InstanceUnavailable,
    SeedNotApproved,
    create_instance_schema,
)
from app.services.product_data_store import ProductDataStore
from app.workspace_config import get_workspace_scope
from products.linear_simplified.backend import demo_seed as seed_module
from products.linear_simplified.backend.lookup import lookup_for
from test_private_demo_api import PRODUCT, PrivateDemoApiTest


class PrivateDemoFixture(unittest.TestCase):
    """The private-demo API fixture, without re-running that file's own tests."""

    setUp = PrivateDemoApiTest.setUp
    visitor = PrivateDemoApiTest.visitor
    data = PrivateDemoApiTest.data


MINUTE = 60


class LookupBindsToThePrivateInstanceTest(PrivateDemoFixture):
    """Finding 1."""

    def visitor_grant(self) -> tuple[RecordGrant, dict[str, str]]:
        body, headers = self.visitor()
        return product_record_grant(visitor_from_token(body["token"]), PRODUCT), headers

    def reassign_to_noah(self, headers):
        issue = next(item for item in self.data(headers)["issues"] if item["id"] == "LIN-142")
        response = self.client.put("/api/demo-data/issues/LIN-142", headers=headers,
                                   json={**issue, "assignee": "Noah Patel"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_visitors_lookup_reads_that_visitors_instance(self):
        grant, headers = self.visitor_grant()
        self.reassign_to_noah(headers)
        issue = lookup_for(grant).get("issue", "LIN-142")
        self.assertIsNotNone(issue)
        self.assertEqual(issue.fields.get("assignee"), "noah-patel")

    def test_another_visitors_lookup_is_unaffected(self):
        first, first_headers = self.visitor_grant()
        second, _ = self.visitor_grant()
        self.reassign_to_noah(first_headers)
        self.assertEqual(lookup_for(second).get("issue", "LIN-142").fields.get("assignee"), "maya-chen")

    def test_a_store_for_another_instance_or_the_shared_store_is_refused(self):
        first, _ = self.visitor_grant()
        second, _ = self.visitor_grant()
        for wrong in (ProductDataStore(second.demo_context), ProductDataStore()):
            with self.subTest(store=wrong.demo_context):
                with self.assertRaises(ValueError):
                    lookup_for(first, wrong)

    def test_a_member_lookup_still_reads_the_member_records(self):
        ProductDataStore().seed_if_empty()
        member = lookup_for(RecordGrant(frozenset(), True))
        self.assertIsNone(member._store.demo_context)


class CapacityCannotBeHeldTest(unittest.TestCase):
    """Finding 2."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        override = patch.object(db, "DB_PATH", Path(temporary.name) / "capacity.sqlite3")
        override.start()
        self.addCleanup(override.stop)
        with db.get_connection() as connection:
            create_instance_schema(connection)
        self.now = 1_000_000.0
        self.store = DemoInstanceStore(
            InstanceLimits(active_per_product=2, reclaim_after_seconds=15 * MINUTE),
            clock=lambda: self.now,
        )
        self.seed = DemoSeed.from_records("seed-1", {"issues": {"LIN-142": {"assignee": "maya"}}})

    def allocate(self, visitor: str):
        return self.store.allocate("tenant-a", "product-a", visitor, self.seed)

    def test_filling_every_slot_does_not_lock_out_later_visitors(self):
        """The reproduction: slots claimed and then left unused no longer block anyone."""
        abandoned = [self.allocate("flood-1"), self.allocate("flood-2")]
        self.now += 16 * MINUTE
        newcomer = self.allocate("real-visitor")
        self.assertEqual(self.store.read(newcomer)["issues"]["LIN-142"]["data"]["assignee"], "maya")
        with self.assertRaises(InstanceUnavailable):
            self.store.read(abandoned[0])  # the one unused longest was reclaimed
        self.store.read(abandoned[1])  # the other still exists

    def test_a_visitor_who_is_using_their_demo_is_never_evicted(self):
        first, second = self.allocate("a"), self.allocate("b")
        self.now += 14 * MINUTE
        self.store.touch(first)
        self.store.touch(second)
        self.now += 14 * MINUTE  # both used within the last 15 minutes
        with self.assertRaises(InstanceCapacity):
            self.allocate("c")
        self.store.read(first)
        self.store.read(second)

    def test_reclaiming_frees_the_longest_unused_first(self):
        first = self.allocate("a")
        self.now += 5 * MINUTE
        second = self.allocate("b")
        self.now += 20 * MINUTE
        self.store.touch(second)
        self.now += 1
        self.allocate("c")
        with self.assertRaises(InstanceUnavailable):
            self.store.read(first)
        self.store.read(second)

    def test_capacity_and_reclaiming_are_reported_to_operators(self):
        self.allocate("a")
        self.allocate("b")
        with self.assertLogs("pixel.demo", level="WARNING") as refused:
            with self.assertRaises(InstanceCapacity):
                self.allocate("c")
        self.assertIn('"event": "demo_capacity_reached"', refused.output[0])
        self.now += 16 * MINUTE
        with self.assertLogs("pixel.demo", level="INFO") as reclaimed:
            self.allocate("d")
        self.assertIn('"event": "demo_instance_reclaimed"', reclaimed.output[0])

    def test_reclaim_must_come_before_idle_expiry(self):
        with self.assertRaises(ValueError):
            InstanceLimits(idle_seconds=600, reclaim_after_seconds=900)

    def test_instances_created_before_the_upgrade_gain_a_last_used_time(self):
        with db.get_connection() as connection:
            connection.execute("drop table demo_instance_receipts")
            connection.execute("drop table demo_instance_records")
            connection.execute("drop table demo_instances")
            connection.execute(
                "create table demo_instances(tenant_id text not null, product_id text not null, "
                "instance_id text not null, visitor_id text not null, generation integer not null, "
                "seed_version text not null, seed_checksum text not null, created_at real not null, "
                "expires_at real not null, idle_expires_at real not null, "
                "primary key (tenant_id, product_id, instance_id))"
            )
            connection.execute(
                "insert into demo_instances values ('t', 'p', 'i', 'v', 1, 's', 'c', 5.0, 9e9, 9e9)"
            )
            create_instance_schema(connection)
            row = connection.execute("select last_active_at from demo_instances").fetchone()
        self.assertEqual(row["last_active_at"], 5.0)


class LookupsHaveNoSharedDefaultTest(unittest.TestCase):
    """Finding 3: a missing snapshot fails; it never reads shared records, seed files or names."""

    def test_every_lookup_refuses_to_run_without_the_callers_records(self):
        calls = {
            "load_demo_issues": lambda: demo_data.load_demo_issues(),
            "find_issue_by_id": lambda: demo_data.find_issue_by_id("LIN-142"),
            "find_issue_by_person": lambda: demo_data.find_issue_by_person("maya's ticket"),
            "team_member_exists": lambda: demo_data.team_member_exists("Maya Chen"),
            "team_member_in_scope": lambda: demo_data.team_member_in_scope("Maya Chen", {"PRJ-101"}),
            "load_team_member_names": lambda: demo_data.load_team_member_names(),
            "get_workspace_scope": lambda: get_workspace_scope("workspace-product-eng"),
        }
        for name, call in calls.items():
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    call()

    def test_lookups_use_exactly_the_records_they_are_given(self):
        data = {"issues": [], "team": [{"name": "Only Person", "projectIds": ["PRJ-1"]}]}
        self.assertEqual(demo_data.load_team_member_names(data), ("Only Person",))
        self.assertFalse(demo_data.team_member_exists("Maya Chen", data))
        self.assertIsNone(demo_data.find_issue_by_id("LIN-142", data))

    def test_no_seed_file_or_built_in_name_list_remains(self):
        source = Path(demo_data.__file__).read_text(encoding="utf-8")
        self.assertNotIn("except Exception", source)
        self.assertNotIn("ISSUES_PATH", source)
        self.assertNotIn('"Noah Patel", "Avery Brooks"', source)


class SeedIsPinnedTest(PrivateDemoFixture):
    """Finding 4."""

    def test_the_installed_seed_matches_its_approved_version_and_checksum(self):
        from app.services.demo_instances import DemoSeed

        approved = DemoSeed.from_records(seed_module.APPROVED_SEED_VERSION,
                                         seed_module.approved_records())
        self.assertEqual((approved.version, approved.checksum),
                         (seed_module.APPROVED_SEED_VERSION, seed_module.APPROVED_SEED_CHECKSUM))

    def test_only_the_cycle_dates_are_added_after_approval(self):
        """Materialising a cycle's window must not be a way to change anything else in the seed."""
        approved = seed_module.approved_records()
        served = json.loads(seed_module.demo_seed().content)
        self.assertEqual(served.keys(), approved.keys())
        for entity, records in approved.items():
            if entity != "cycles":
                self.assertEqual(served[entity], records, entity)
        for key, cycle in approved["cycles"].items():
            added = {"startDate", "endDate", "daysLeft"}
            removed = {"startsInDays", "endsInDays"}
            self.assertEqual(set(served["cycles"][key]) - set(cycle), added, key)
            self.assertEqual(set(cycle) - set(served["cycles"][key]), removed, key)
            unchanged = {name: value for name, value in cycle.items() if name not in removed}
            self.assertEqual({name: served["cycles"][key][name] for name in unchanged}, unchanged, key)

    def edited_issues_file(self) -> Path:
        issues = json.loads(seed_module.ISSUES_PATH.read_text(encoding="utf-8"))
        issues[0] = {**issues[0], "title": "Quietly edited"}
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "issues.json"
        path.write_text(json.dumps(issues), encoding="utf-8")
        return path

    def test_an_unreviewed_seed_change_is_refused(self):
        with patch.object(seed_module, "ISSUES_PATH", self.edited_issues_file()):
            with self.assertRaises(SeedNotApproved):
                seed_module.demo_seed()

    def test_no_new_visitor_starts_from_an_unapproved_seed(self):
        with patch.object(seed_module, "ISSUES_PATH", self.edited_issues_file()):
            response = self.client.post(f"/api/organizations/pixel-dev/products/{PRODUCT}/visitor-sessions")
        self.assertEqual(response.status_code, 404)
        with db.get_connection() as connection:
            self.assertEqual(connection.execute("select count(*) from demo_instances").fetchone()[0], 0)

    def test_an_unapproved_seed_cannot_be_used_for_a_reset(self):
        _, headers = self.visitor()
        with patch.object(seed_module, "ISSUES_PATH", self.edited_issues_file()):
            response = self.client.post("/api/demo-data/reset-mine", headers=headers)
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
