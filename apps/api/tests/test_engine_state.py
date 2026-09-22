"""Milestone 3.2, Slice 5c step 2: durable definition-engine state (plan, section 6).

Unit-level coverage for `EngineStateStore` and `rebuild_signal_history`, independent of the turn
service that wires them in (`test_turn_execution.py` covers restart continuity end to end).
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db
from app.engine.actions import RecordRef
from app.engine.execution import ExecutionOwner
from app.engine.memory import ConversationMemory, PendingConfirmation, PersonFollowUp
from app.engine.signals import EngineSignal, SignalHistory
from app.services.engine_state import CODEC_VERSION, EnginePin, EngineStateStore, PendingStateCache, \
    rebuild_signal_history
from app.services import env as env_module

PIN = EnginePin("linear_simplified", 3, "checksum-a", 1)
OTHER_PIN = EnginePin("linear_simplified", 3, "checksum-b", 1)


class EngineStateFixture(unittest.TestCase):
    def setUp(self):
        files_patch = patch.object(env_module, "_env_files", lambda: ())
        files_patch.start()
        self.addCleanup(files_patch.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = patch.object(db, "DB_PATH", Path(temporary.name) / "engine_state.sqlite3")
        database.start()
        self.addCleanup(database.stop)
        db.migrate()
        self.store = EngineStateStore()

    def make_session(self, session_id: str, owner: ExecutionOwner, scope_id: str = "scope-1") -> None:
        with db.get_connection() as connection:
            connection.execute(
                "insert into sessions(id, product_id, started_at) values (?, ?, ?)",
                (session_id, owner.product_id, "2026-01-01T00:00:00Z"),
            )
            connection.execute(
                "insert into conversation_owners(session_id, user_id, customer_id, product_id, scope_id, "
                "instance_id, instance_generation) values (?, ?, ?, ?, ?, ?, ?)",
                (session_id, owner.user_id, owner.tenant_id, owner.product_id, scope_id,
                 owner.instance_id, owner.instance_generation),
            )


class CommitAndLoadTest(EngineStateFixture):
    def setUp(self):
        super().setUp()
        self.owner = ExecutionOwner("tenant-a", "product-a", "user-a")
        self.make_session("s1", self.owner)

    def test_a_fresh_session_has_no_durable_state(self):
        with db.get_connection() as connection:
            loaded = self.store.load(connection, self.owner, "s1", "scope-1", PIN)
        self.assertIsNone(loaded)

    def test_committed_identifiers_are_loaded_back(self):
        memory = ConversationMemory(
            focus=RecordRef("issue", "LIN-142"), last_person=RecordRef("member", "Maya Chen"),
            last_view="issues", last_change="key-1", turn=1,
            person_follow_up=PersonFollowUp("open_issue", 1),
        )
        with db.get_connection() as connection:
            connection.execute("begin immediate")
            revision = self.store.commit(connection, self.owner, "s1", 1, "scope-1", PIN, memory,
                                         expected_revision=0)
        self.assertEqual(revision, 1)
        with db.get_connection() as connection:
            loaded = self.store.load(connection, self.owner, "s1", "scope-1", PIN)
        self.assertEqual(loaded.memory.focus, RecordRef("issue", "LIN-142"))
        self.assertEqual(loaded.memory.last_person, RecordRef("member", "Maya Chen"))
        self.assertEqual(loaded.memory.last_view, "issues")
        self.assertEqual(loaded.memory.last_change, "key-1")
        self.assertEqual(loaded.memory.turn, 1)
        self.assertEqual(loaded.memory.person_follow_up, PersonFollowUp("open_issue", 1))
        self.assertEqual(loaded.revision, 1)
        self.assertFalse(loaded.pending_requires_repeat)

    def test_pending_state_is_never_stored_only_a_marker(self):
        memory = ConversationMemory(
            pending_confirmation=PendingConfirmation(action=None, reason=None, turn=1), turn=1,
        )
        with db.get_connection() as connection:
            connection.execute("begin immediate")
            self.store.commit(connection, self.owner, "s1", 1, "scope-1", PIN, memory, expected_revision=0)
        with db.get_connection() as connection:
            loaded = self.store.load(connection, self.owner, "s1", "scope-1", PIN)
            row = connection.execute(
                "select * from engine_state where session_id = 's1'"
            ).fetchone()
        self.assertTrue(loaded.pending_requires_repeat)
        # Nothing about the confirmation's action or reason is a column here at all.
        self.assertNotIn("action", row.keys())
        self.assertIsNone(loaded.memory.pending_confirmation)
        self.assertIsNone(loaded.memory.pending_clarification)

    def test_a_stale_revision_is_refused(self):
        memory = ConversationMemory(turn=1)
        with db.get_connection() as connection:
            connection.execute("begin immediate")
            self.store.commit(connection, self.owner, "s1", 1, "scope-1", PIN, memory, expected_revision=0)
        # A second writer thinks it is still working from revision 0 (a lost race).
        with db.get_connection() as connection:
            connection.execute("begin immediate")
            result = self.store.commit(connection, self.owner, "s1", 2, "scope-1", PIN,
                                       ConversationMemory(turn=2), expected_revision=0)
        self.assertIsNone(result)
        with db.get_connection() as connection:
            loaded = self.store.load(connection, self.owner, "s1", "scope-1", PIN)
        self.assertEqual(loaded.memory.turn, 1, "the newer write must not be overwritten")

    def test_an_out_of_order_turn_cannot_overwrite_newer_state(self):
        with db.get_connection() as connection:
            connection.execute("begin immediate")
            self.store.commit(connection, self.owner, "s1", 5, "scope-1", PIN, ConversationMemory(turn=5),
                              expected_revision=0)
        with db.get_connection() as connection:
            loaded_before = self.store.load(connection, self.owner, "s1", "scope-1", PIN)
            connection.execute("begin immediate")
            # A delayed turn 3 arrives after turn 5 already committed.
            result = self.store.commit(connection, self.owner, "s1", 3, "scope-1", PIN,
                                       ConversationMemory(turn=3), expected_revision=loaded_before.revision)
        self.assertIsNone(result)


class IsolationTest(EngineStateFixture):
    def test_a_row_for_a_different_owner_is_never_returned(self):
        owner_a = ExecutionOwner("tenant-a", "product-a", "user-a")
        owner_b = ExecutionOwner("tenant-b", "product-a", "user-b")
        self.make_session("shared-id", owner_a)
        with db.get_connection() as connection:
            connection.execute("begin immediate")
            self.store.commit(connection, owner_a, "shared-id", 1, "scope-1", PIN,
                              ConversationMemory(focus=RecordRef("issue", "LIN-1"), turn=1),
                              expected_revision=0)
        with db.get_connection() as connection:
            loaded = self.store.load(connection, owner_b, "shared-id", "scope-1", PIN)
        self.assertIsNone(loaded)

    def test_a_different_private_instance_generation_is_never_returned(self):
        generation_one = ExecutionOwner("tenant-a", "product-a", "user-a", "instance-1", 1)
        generation_two = ExecutionOwner("tenant-a", "product-a", "user-a", "instance-1", 2)
        self.make_session("s1", generation_one)
        with db.get_connection() as connection:
            connection.execute("begin immediate")
            self.store.commit(connection, generation_one, "s1", 1, "scope-1", PIN,
                              ConversationMemory(turn=1), expected_revision=0)
        with db.get_connection() as connection:
            loaded = self.store.load(connection, generation_two, "s1", "scope-1", PIN)
        self.assertIsNone(loaded)

    def test_a_workspace_switch_clears_remembered_references(self):
        owner = ExecutionOwner("tenant-a", "product-a", "user-a")
        self.make_session("s1", owner)
        with db.get_connection() as connection:
            connection.execute("begin immediate")
            self.store.commit(connection, owner, "s1", 1, "scope-1", PIN,
                              ConversationMemory(focus=RecordRef("issue", "LIN-1"), turn=1),
                              expected_revision=0)
        with db.get_connection() as connection:
            loaded = self.store.load(connection, owner, "s1", "scope-2", PIN)
        self.assertIsNone(loaded)

    def test_a_pin_mismatch_fails_closed(self):
        owner = ExecutionOwner("tenant-a", "product-a", "user-a")
        self.make_session("s1", owner)
        with db.get_connection() as connection:
            connection.execute("begin immediate")
            self.store.commit(connection, owner, "s1", 1, "scope-1", PIN, ConversationMemory(turn=1),
                              expected_revision=0)
        with db.get_connection() as connection:
            loaded = self.store.load(connection, owner, "s1", "scope-1", OTHER_PIN)
        self.assertIsNone(loaded)

    def test_invalidate_removes_the_row(self):
        owner = ExecutionOwner("tenant-a", "product-a", "user-a")
        self.make_session("s1", owner)
        with db.get_connection() as connection:
            connection.execute("begin immediate")
            self.store.commit(connection, owner, "s1", 1, "scope-1", PIN, ConversationMemory(turn=1),
                              expected_revision=0)
        with db.get_connection() as connection:
            self.store.invalidate_sessions(connection, ["s1"])
        with db.get_connection() as connection:
            loaded = self.store.load(connection, owner, "s1", "scope-1", PIN)
        self.assertIsNone(loaded)


class SignalHistoryRebuildTest(EngineStateFixture):
    def test_only_this_owner_scope_and_session_are_included(self):
        owner = ExecutionOwner("tenant-a", "product-a", "user-a")
        self.make_session("s1", owner, scope_id="scope-1")
        other_owner = ExecutionOwner("tenant-b", "product-a", "user-b")
        self.make_session("s2", other_owner, scope_id="scope-1")
        with db.get_connection() as connection:
            connection.execute(
                "insert into signals(id, session_id, turn_id, type, value, confidence, created_at, scope_id) "
                "values ('a', 's1', 1, 'feature_interest', 'issues', 0.7, 't1', 'scope-1')"
            )
            # Another workspace in the same session: excluded.
            connection.execute(
                "insert into signals(id, session_id, turn_id, type, value, confidence, created_at, scope_id) "
                "values ('b', 's1', 2, 'feature_interest', 'cycles', 0.7, 't2', 'scope-2')"
            )
            # A legacy row with no workspace at all: excluded.
            connection.execute(
                "insert into signals(id, session_id, turn_id, type, value, confidence, created_at, scope_id) "
                "values ('c', 's1', 3, 'feature_interest', 'projects', 0.7, 't3', null)"
            )
            # Another owner's session: excluded even though the workspace name matches.
            connection.execute(
                "insert into signals(id, session_id, turn_id, type, value, confidence, created_at, scope_id) "
                "values ('d', 's2', 1, 'feature_interest', 'teams', 0.7, 't1', 'scope-1')"
            )
            history = rebuild_signal_history(connection, owner, "s1", "scope-1")
        self.assertEqual(history.values("feature_interest"), ("issues",))


class PendingStateCacheTest(unittest.TestCase):
    def test_the_matching_revision_is_a_hit(self):
        cache = PendingStateCache()
        owner = ExecutionOwner("tenant-a", "product-a", "user-a")
        cache.put(owner, "s1", revision=1, memory=ConversationMemory(turn=1), history=SignalHistory())
        self.assertIsNotNone(cache.get(owner, "s1", revision=1))

    def test_a_revision_mismatch_is_a_miss_and_discards_the_stale_entry(self):
        cache = PendingStateCache()
        owner = ExecutionOwner("tenant-a", "product-a", "user-a")
        cache.put(owner, "s1", revision=1, memory=ConversationMemory(turn=1), history=SignalHistory())
        self.assertIsNone(cache.get(owner, "s1", revision=2))
        # The stale entry is gone even for the revision it was originally cached under.
        self.assertIsNone(cache.get(owner, "s1", revision=1))

    def test_idle_expiry_discards_the_entry(self):
        clock = {"now": 0.0}
        cache = PendingStateCache(idle_seconds=60, clock=lambda: clock["now"])
        owner = ExecutionOwner("tenant-a", "product-a", "user-a")
        cache.put(owner, "s1", revision=1, memory=ConversationMemory(turn=1), history=SignalHistory())
        clock["now"] = 61
        self.assertIsNone(cache.get(owner, "s1", revision=1))

    def test_over_the_limit_evicts_the_oldest_entry(self):
        cache = PendingStateCache(limit=2)
        owner = ExecutionOwner("tenant-a", "product-a", "user-a")
        for index, session in enumerate(("s1", "s2", "s3")):
            cache.put(owner, session, revision=1, memory=ConversationMemory(turn=index), history=SignalHistory())
        self.assertIsNone(cache.get(owner, "s1", revision=1))
        self.assertIsNotNone(cache.get(owner, "s3", revision=1))


if __name__ == "__main__":
    unittest.main()
