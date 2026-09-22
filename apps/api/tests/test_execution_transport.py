"""Milestone 3.2, Slice 5b steps 2 to 4: storage, ledger primitives and keyed writes.

Plan sections 6 to 9 and the S1, S2 and S4 tests of section 13: supersession at every accepted
activation, exact-turn owner-bound cancellation, durable result codes and receipts, workspace
binding, private instances, settlement triggers, migration of a populated old schema, the startup
gate and retention.
"""
from __future__ import annotations

from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, main, ops  # noqa: E402
from app.auth import visitor_from_token  # noqa: E402
from app.engine.actions import GenericAction, RecordRef  # noqa: E402
from app.engine.composer import RECEIPT_FAILURES, receipt_speech  # noqa: E402
from app.engine.execution import (  # noqa: E402
    RETENTION_SECONDS,
    ExecutionLedger,
    ExecutionOwner,
    LegacyKeysPresent,
    principal_owner,
    require_no_legacy_keys,
)
from app.engine.validator import ValidatedAction  # noqa: E402
from test_execution_boundary import (  # noqa: E402
    OTHER_USER,
    PRODUCT,
    SCOPE,
    TENANT,
    USER,
    ExecutionFixture,
    create_fields,
)
from test_private_demo_api import PrivateDemoApiTest  # noqa: E402

OTHER_SCOPE = "workspace-platform"


class SupersessionAtActivationTest(ExecutionFixture):
    """S1: supersession belongs to every accepted activation, not to dispatch."""

    def activate(self, turn_id: int, owner: ExecutionOwner | None = None, scope: str = SCOPE) -> bool:
        return self.sessions.activate_turn("s-exec", turn_id, owner=owner or self.owner(), scope_id=scope)

    def recorded_scope(self, session_id: str = "s-exec") -> str:
        return self.read("select scope_id from conversation_owners where session_id = ?", (session_id,))[0]["scope_id"]

    def test_a_newer_question_turn_supersedes_the_older_key_before_it_finishes(self):
        self.assertTrue(self.activate(1))
        key = self.dispatch_create(turn_id=1)
        self.assertTrue(self.activate(2), "turn 2 is only a question; it never dispatches")
        row = self.key_row(key)
        self.assertEqual((row["state"], row["result_code"]), ("cancelled", "superseded"))
        body = self.assert_receipt(self.post_keyed(key), 409, "refused", "superseded")
        self.assertEqual(body["speech"], "A newer request replaced this change, so it wasn't applied.")
        self.assertEqual(self.keyed_count(), 0)

    def test_activation_without_keys_writes_no_ledger_rows(self):
        self.assertTrue(self.activate(1))
        self.assertTrue(self.activate(2))
        self.assertEqual(self.execution_rows(), [])

    def test_a_stale_activation_changes_nothing(self):
        self.assertTrue(self.activate(1))
        key = self.dispatch_create(turn_id=1)
        self.assertTrue(self.activate(3, scope=SCOPE))
        before = self.key_row(key)["state"]
        self.assertFalse(self.activate(2, scope=OTHER_SCOPE), "an older turn is stale")
        self.assertEqual(self.recorded_scope(), SCOPE, "a delayed stale turn cannot move the workspace")
        self.assertEqual(self.key_row(key)["state"], before)

    def test_ensure_session_never_moves_an_existing_sessions_workspace(self):
        self.assertTrue(self.sessions.ensure_session("s-exec", PRODUCT, USER.user_id, TENANT, OTHER_SCOPE))
        self.assertEqual(self.recorded_scope(), SCOPE)
        self.assertTrue(self.activate(1, scope=OTHER_SCOPE))
        self.assertEqual(self.recorded_scope(), OTHER_SCOPE, "only an accepted activation moves it")

    def test_executed_keys_are_never_cancelled(self):
        self.assertTrue(self.activate(1))
        key = self.dispatch_create(turn_id=1)
        self.assert_receipt(self.post_keyed(key), 200, "executed", "applied")
        self.assertTrue(self.activate(2))
        self.assertEqual((self.key_row(key)["state"], self.key_row(key)["result_code"]), ("executed", "applied"))

    def test_another_owner_cannot_activate_or_cancel_on_the_same_session_id(self):
        self.assertTrue(self.activate(1))
        key = self.dispatch_create(turn_id=1)
        stranger = ExecutionOwner(TENANT, PRODUCT, OTHER_USER.user_id)
        self.assertFalse(self.activate(2, owner=stranger))
        self.assertFalse(self.sessions.cancel_turn("s-exec", 1, owner=stranger))
        self.assertEqual(self.key_row(key)["state"], "dispatched")

    def test_dispatch_happens_only_while_the_turn_is_active_in_its_workspace(self):
        validated = self.validated("update_issue", target=RecordRef("issue", "PIX-1"), fields={"priority": "Low"})
        change = {"action": "update_issue", "target": "PIX-1", "changes": {"priority": "Low"}}
        self.assertTrue(self.activate(1))
        self.assertTrue(self.activate(2))
        dispatch = lambda turn, scope=SCOPE: self.ledger.dispatch_if_active(  # noqa: E731
            validated, self.owner(), session_id="s-exec", turn_id=turn, scope_id=scope, change_set=change)
        self.assertIsNone(dispatch(1), "a superseded turn gets no key")
        self.assertIsNone(dispatch(2, OTHER_SCOPE), "a workspace other than the recorded one gets no key")
        self.assertIsNotNone(dispatch(2))
        stranger = ExecutionOwner(TENANT, PRODUCT, OTHER_USER.user_id)
        self.assertIsNone(self.ledger.dispatch_if_active(validated, stranger, session_id="s-exec", turn_id=2,
                                                         scope_id=SCOPE, change_set=change))

    def test_dispatch_if_current_survives_its_own_turns_completion_but_not_a_newer_ones(self):
        """The legacy authority adapter (5c plan, section 3.3) asks after its own turn has already
        completed and cleared `active_turn_id`, so it is checked against `latest_turn_id` instead."""
        validated = self.validated("update_issue", target=RecordRef("issue", "PIX-1"), fields={"priority": "Low"})
        change = {"action": "update_issue", "target": "PIX-1", "changes": {"priority": "Low"}}
        self.assertTrue(self.activate(1))
        self.sessions.complete_turn("s-exec", 1)
        dispatch = lambda turn, scope=SCOPE: self.ledger.dispatch_if_current(  # noqa: E731
            validated, self.owner(), session_id="s-exec", turn_id=turn, scope_id=scope, change_set=change)
        self.assertIsNotNone(dispatch(1), "still the latest turn, even though it already completed")

        self.assertTrue(self.activate(2))
        self.sessions.complete_turn("s-exec", 2)
        self.assertIsNone(dispatch(1), "turn 1 is no longer the latest turn")
        self.assertIsNone(dispatch(2, OTHER_SCOPE), "a workspace other than the recorded one gets no key")
        self.assertIsNotNone(dispatch(2))

    def test_cancel_affects_exactly_the_named_turn(self):
        self.assertTrue(self.activate(1))
        key = self.dispatch_create(turn_id=1)
        self.sessions.cancel_turn("s-exec", 5, owner=self.owner())
        self.assertEqual(self.key_row(key)["state"], "dispatched", "a fabricated higher turn cancels nothing")
        self.sessions.cancel_turn("s-exec", 1, owner=self.owner())
        self.assertEqual((self.key_row(key)["state"], self.key_row(key)["result_code"]),
                         ("cancelled", "user_cancelled"))

    def test_two_turns_racing_leave_exactly_one_usable_key_the_newest(self):
        validated = self.validated("update_issue", target=RecordRef("issue", "PIX-1"), fields={"priority": "Low"})
        change = {"action": "update_issue", "target": "PIX-1", "changes": {"priority": "Low"}}
        for first, second in ((1, 2), (4, 3)):
            barrier = threading.Barrier(2)
            keys: dict[int, object] = {}

            def turn(number: int) -> None:
                barrier.wait()
                if self.activate(number):
                    keys[number] = self.ledger.dispatch_if_active(
                        validated, self.owner(), session_id="s-exec", turn_id=number, scope_id=SCOPE,
                        change_set=change)

            threads = [threading.Thread(target=turn, args=(first,)), threading.Thread(target=turn, args=(second,))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            usable = self.read("select turn_id from action_executions where state = 'dispatched'")
            newest = max(first, second)
            self.assertEqual([row["turn_id"] for row in usable], [newest] if newest in keys else [])
            with db.get_connection() as connection:
                connection.execute("begin immediate")
                self.ledger.supersede(connection, self.owner(), "s-exec", 10 ** 6)


class ReceiptWordingTest(unittest.TestCase):
    """S2: every code has its sentence, and replays never invent values."""

    def test_every_failure_code_has_its_platform_sentence(self):
        for code, sentence in RECEIPT_FAILURES.items():
            with self.subTest(code=code):
                self.assertEqual(receipt_speech(code, executed=False, replay=False), sentence)
        self.assertEqual(receipt_speech("some_new_rule", executed=False, replay=True),
                         RECEIPT_FAILURES["invalid_change"])

    def test_a_first_execution_names_only_the_bound_values_and_a_replay_names_none(self):
        first = receipt_speech("applied", executed=True, replay=False, record_id="LIN-142",
                               changes={"assignee": "Noah Patel"})
        self.assertEqual(first, "Updated LIN-142: assignee to Noah Patel.")
        replay = receipt_speech("applied", executed=True, replay=True, record_id="LIN-142",
                                changes={"assignee": "Noah Patel"})
        self.assertEqual(replay, "This change was already applied.")


class DurableOutcomeTest(ExecutionFixture):
    """S2: outcomes come from committed state; no receipt text or value reaches the ledger."""

    def test_no_receipt_text_or_field_value_is_stored(self):
        key = self.dispatch_create()
        self.post_keyed(key)
        failed = self.dispatch_update("LIN-404", {"priority": "Low"}, turn_id=2)
        self.patch_keyed(failed, "LIN-404", {"priority": "Low"})
        stored = json.dumps([tuple(row) for row in self.execution_rows()])
        for text in ("Created", "wasn't applied", "Keyed write", "Maya Chen", "Low"):
            self.assertNotIn(text, stored)

    def test_the_database_refuses_a_settled_row_without_a_result_code(self):
        key = self.dispatch_create()
        with closing(sqlite3.connect(self.db_path)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("update action_executions set state = 'executed' where execution_key = ?", (key,))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "insert into action_executions(execution_key, tenant_id, product_id, session_id, turn_id, "
                    "user_id, action_key, capability, request_digest, state, created_at, expires_at) "
                    "values ('x', 't', 'p', 's', 1, 'u', 'a', 'c', 'd', 'cancelled', 'now', 0)")


class WorkspaceBindingTest(ExecutionFixture):
    """S4: a key writes only inside the workspace it was issued for."""

    def platform_issue(self) -> str:
        rows = self.read("select id from demo_issues where project in ('Planning', 'Migration') limit 1")
        return rows[0]["id"]

    def test_a_key_cannot_touch_a_record_in_another_workspace(self):
        grant_all = "update record_grants set scope_ids = ?, is_admin = 0 where user_id = ?"
        with db.get_connection() as connection:
            connection.execute(grant_all, (json.dumps([SCOPE, OTHER_SCOPE]), USER.user_id))
        issue_id = self.platform_issue()
        key = self.dispatch_update(issue_id, {"priority": "Low"})
        self.assert_receipt(self.patch_keyed(key, issue_id, {"priority": "Low"}), 409, "failed", "scope_mismatch")

    def test_a_create_takes_its_workspace_from_the_key_only(self):
        key = self.dispatch_create(create_fields(project="Planning"))
        self.assert_receipt(self.post_keyed(key, create_fields(project="Planning")), 409, "failed", "invalid_change")
        ok = self.dispatch_create(turn_id=2)
        record = self.assert_receipt(self.post_keyed(ok), 200, "executed", "applied")["record"]
        self.assertEqual(record["project"], "Integrations")

    def test_history_is_read_only_in_the_current_workspace(self):
        key = self.dispatch_create()
        self.assert_receipt(self.post_keyed(key), 200, "executed", "applied")
        self.assertIsNotNone(self.ledger.last_executed(self.owner(), "s-exec", SCOPE))
        self.assertIsNone(self.ledger.last_executed(self.owner(), "s-exec", OTHER_SCOPE))
        stranger = ExecutionOwner(TENANT, PRODUCT, OTHER_USER.user_id)
        self.assertIsNone(self.ledger.last_executed(stranger, "s-exec", SCOPE))


class PrivateInstanceTest(unittest.TestCase):
    """B1: a visitor's keyed write lands in that visitor's instance and nowhere else."""

    setUp = PrivateDemoApiTest.setUp
    visitor = PrivateDemoApiTest.visitor
    data = PrivateDemoApiTest.data
    turn = PrivateDemoApiTest.turn

    def keyed_update(self, headers: dict, token: str, session_id: str, turn_id: int = 1) -> tuple[str, dict]:
        principal = visitor_from_token(token)
        owner = principal_owner(principal, PRODUCT)
        definition = main.agent.directory.definitions.load("linear_simplified", 2).definition
        validated = ValidatedAction(GenericAction.for_definition(
            definition, "update_issue", target=RecordRef("issue", "LIN-142"), fields={"priority": "Low"},
        ), "linear_simplified", 2)
        key = ExecutionLedger().dispatch_if_active(
            validated, owner, session_id=session_id, turn_id=turn_id, scope_id=SCOPE,
            change_set={"action": "update_issue", "target": "LIN-142", "changes": {"priority": "Low"}},
        )
        self.assertIsNotNone(key)
        return key.execution_key, {**headers, "X-Execution-Key": key.execution_key, "X-Session-Id": session_id}

    def priority(self, headers: dict) -> str:
        return next(item for item in self.data(headers)["issues"] if item["id"] == "LIN-142")["priority"]

    def open_session(self, headers: dict) -> str:
        session_id = f"visitor-{time.time_ns()}"
        self.turn(headers, "Show me the issues", session_id=session_id)
        with db.get_connection() as connection:
            connection.execute("update sessions set active_turn_id = 1 where id = ?", (session_id,))
        return session_id

    def test_the_write_changes_only_this_visitors_instance(self):
        body, headers = self.visitor()
        _, other_headers = self.visitor()
        before_other = self.priority(other_headers)
        session_id = self.open_session(headers)
        _, keyed = self.keyed_update(headers, body["token"], session_id)
        response = self.client.patch("/api/demo-data/issues/LIN-142", json={"changes": {"priority": "Low"}},
                                     headers=keyed)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.priority(headers), "Low")
        self.assertEqual(self.priority(other_headers), before_other, "another visitor is untouched")
        member = db.get_connection
        with member() as connection:
            shared = connection.execute("select priority from demo_issues where id = 'LIN-142'").fetchone()
        self.assertNotEqual(shared and shared["priority"], "Low", "the member records are untouched")

    def test_a_key_from_before_a_private_reset_is_an_unknown_key(self):
        body, headers = self.visitor()
        session_id = self.open_session(headers)
        key, keyed = self.keyed_update(headers, body["token"], session_id)
        reset = self.client.post("/api/demo-data/reset-mine", headers=headers)
        self.assertEqual(reset.status_code, 200, reset.text)
        with db.get_connection() as connection:
            row = connection.execute("select state, result_code from action_executions where execution_key = ?",
                                     (key,)).fetchone()
        self.assertEqual((row["state"], row["result_code"]), ("cancelled", "instance_reset"))
        new_headers = {"Authorization": f"Bearer {reset.json()['token']}",
                       "X-Execution-Key": key, "X-Session-Id": session_id}
        response = self.client.patch("/api/demo-data/issues/LIN-142", json={"changes": {"priority": "Low"}},
                                     headers=new_headers)
        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotIn("outcome", response.json())


OLD_EXECUTIONS_TABLE = """
create table action_executions(
  execution_key text primary key, tenant_id text not null, product_id text not null,
  session_id text not null, turn_id integer not null, user_id text not null, action_key text not null,
  capability text not null, entity text, target_id text, request_digest text not null,
  state text not null check (state in ('dispatched', 'executed', 'failed', 'cancelled')),
  result_record_id text, result_code text, reason text, created_at text not null,
  expires_at real not null, settled_at text
)
"""


class MigrationAndGateTest(unittest.TestCase):
    """A populated pre-5b schema migrates additively; the gate refuses a live legacy key."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "old.sqlite3"
        patcher = patch.object(db, "DB_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute(OLD_EXECUTIONS_TABLE)
            connection.executemany(
                "insert into action_executions(execution_key, tenant_id, product_id, session_id, turn_id, user_id, "
                "action_key, capability, request_digest, state, result_code, reason, created_at, expires_at, "
                "settled_at) values (?, 't', 'p', 's', 1, 'u', 'a', 'c', 'd', ?, ?, ?, 'then', ?, ?)",
                [("legacy-settled", "cancelled", None, "demo_reset", time.time(), "2026-01-01T00:00:00"),
                 ("legacy-live", "dispatched", None, None, time.time() + 600, None)],
            )
            connection.commit()

    def test_the_migration_keeps_every_row_and_rewrites_none(self):
        db.migrate()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = {row["execution_key"]: row for row in connection.execute("select * from action_executions")}
        self.assertEqual(set(rows), {"legacy-settled", "legacy-live"})
        self.assertIsNone(rows["legacy-settled"]["result_code"], "legacy settled rows are not rewritten")
        self.assertIsNone(rows["legacy-live"]["scope_id"])

    def test_the_preflight_and_the_startup_gate_refuse_a_live_legacy_key(self):
        db.migrate()
        output = io.StringIO()
        with redirect_stdout(output):
            code = ops.main(["execution-preflight"])
        report = json.loads(output.getvalue())
        self.assertEqual((code, report["ready"], report["legacy_dispatched"]), (1, False, 1))
        with self.assertRaises(LegacyKeysPresent):
            require_no_legacy_keys()
        with patch.object(main, "prune_execution_ledger") as prune:
            with self.assertRaises(LegacyKeysPresent):
                with TestClient(main.app):
                    pass
            prune.assert_not_called()
        with closing(sqlite3.connect(self.path)) as connection:
            count = connection.execute("select count(*) from action_executions").fetchone()[0]
        self.assertEqual(count, 2, "pruning never ran before the gate, so the evidence remains")

    def test_a_legacy_key_is_never_claimable(self):
        db.migrate()
        with db.get_connection() as connection:
            connection.execute("begin immediate")
            with self.assertRaises(Exception) as refused:
                ExecutionLedger().claim(connection, "legacy-live", ExecutionOwner("t", "p", "u"),
                                        request={}, session_id="s")
        self.assertTrue(getattr(refused.exception, "not_found", False))

    def test_a_clean_deployment_passes_the_gate(self):
        db.migrate()
        with db.get_connection() as connection:
            connection.execute("update action_executions set state = 'cancelled', result_code = 'expired', "
                               "settled_at = '2026-01-01T00:00:00' where execution_key = 'legacy-live'")
        self.assertEqual(require_no_legacy_keys()["legacy_dispatched"], 0)


class RetentionTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(temporary.name) / "retention.sqlite3")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.migrate()

    def insert(self, key: str, *, state: str, settled_days: float | None, expires_days: float) -> None:
        now = time.time()
        settled = None if settled_days is None else time.strftime(
            "%Y-%m-%dT%H:%M:%S", time.gmtime(now - settled_days * 86400))
        with db.get_connection() as connection:
            connection.execute(
                "insert into action_executions(execution_key, tenant_id, product_id, session_id, turn_id, user_id, "
                "scope_id, action_key, capability, request_digest, state, result_code, created_at, expires_at, "
                "settled_at) values (?, 't', 'p', 's', 1, 'u', 'w', 'a', 'c', 'd', ?, ?, 'then', ?, ?)",
                (key, state, None if state == "dispatched" else "applied", now - expires_days * 86400, settled),
            )

    def keys(self) -> set[str]:
        with db.get_connection() as connection:
            return {row["execution_key"] for row in connection.execute("select execution_key from action_executions")}

    def test_rows_are_kept_thirty_days_after_settling_or_expiring(self):
        self.insert("old-settled", state="executed", settled_days=31, expires_days=31)
        self.insert("recent-settled", state="executed", settled_days=29, expires_days=29)
        self.insert("old-unused", state="dispatched", settled_days=None, expires_days=31)
        self.insert("recent-unused", state="dispatched", settled_days=None, expires_days=1)
        removed = ExecutionLedger.prune()
        self.assertEqual(removed, 2)
        self.assertEqual(self.keys(), {"recent-settled", "recent-unused"})

    def test_pruning_is_bounded_and_uses_its_indexes(self):
        for index in range(12):
            self.insert(f"k{index}", state="failed", settled_days=40, expires_days=40)
        self.assertEqual(ExecutionLedger.prune(batch=5, batches=2), 10)
        self.assertEqual(len(self.keys()), 2)
        with db.get_connection() as connection:
            settled = " ".join(row[3] for row in connection.execute(
                "explain query plan select rowid from action_executions where settled_at is not null "
                "and settled_at < ? order by settled_at limit 5", ("2026-01-01",)))
            unused = " ".join(row[3] for row in connection.execute(
                "explain query plan select rowid from action_executions where state = 'dispatched' "
                "and expires_at < ? order by expires_at limit 5", (0,)))
        self.assertIn("action_executions_settled", settled)
        self.assertIn("action_executions_unsettled_expiry", unused)
        self.assertEqual(RETENTION_SECONDS, 30 * 24 * 60 * 60)

    def test_the_turn_endpoint_schedules_retention_at_most_once_a_minute_with_the_shadow_off(self):
        schedule = main.RetentionSchedule()
        tasks: list = []
        self.assertTrue(schedule.schedule(tasks.append))
        self.assertFalse(schedule.schedule(tasks.append))
        self.assertEqual(tasks, [main.prune_execution_ledger])
        self.assertIn("retention.schedule(background.add_task)", Path(main.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
