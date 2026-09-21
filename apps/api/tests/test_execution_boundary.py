"""The execution boundary: one transaction, one write, one outcome (3.2 plan, section 5).

These tests exist because the dangerous part of the generic engine is not deciding what to do,
it is doing it exactly once. Each test drives the real HTTP endpoint against a real SQLite file,
because the guarantee is a database guarantee, not a Python one.

What is proven here:

- a key writes once, and presenting it again returns the first outcome instead of repeating it;
- two callers racing the same key produce one record;
- a key belonging to someone else is indistinguishable from one that does not exist;
- cancellation and execution race on a commit, in either order, and never both win;
- a rule rejection commits as `failed`; an unexpected error commits nothing at all;
- everything the caller was allowed to do is re-checked inside the write transaction;
- writes without a key behave exactly as they did before.
"""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from contextlib import closing
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app import db
from app.auth import AuthUser, create_token
from app.definitions.organizations import OrganizationDirectory
from app.definitions.registry import DefinitionRegistry
from app.definitions.sessions import pin_new_session
from app.definitions.access import authorize_product
from app.engine.actions import GenericAction, RecordRef
from app.engine.execution import ExecutionLedger, ExecutionOwner, KEY_LIFETIME_SECONDS
from app.engine.validator import ValidatedAction
from app.main import app, product_data
from app.record_access import grant_records, record_grant
from app.record_schemas import IssueInput
from app.services import env as env_module
from app.services.session_manager import SessionManager

TENANT = "pixel-dev"
PRODUCT = "linear-demo"
SCOPE = "workspace-product-eng"
USER = AuthUser(kind="member", user_id="demo-product-eng", tenant_id=TENANT, role="team_member",
                team_id="planning-team")
OTHER_USER = AuthUser(kind="member", user_id="demo-support", tenant_id=TENANT, role="team_member",
                      team_id="planning-team")

# Every table holding product records, for proving a refused request changed nothing.
RECORD_TABLES = ("demo_issues", "demo_projects", "demo_cycles", "demo_team_members",
                 "demo_workspace_scopes")

NEW_ISSUE = {
    "id": "LIN-900", "title": "Keyed write", "priority": "High", "assignee": "Maya Chen",
    "project": "Integrations", "status": "Todo",
}


def issue_payload(**overrides) -> dict:
    """Exactly the body the endpoint parses, so the key authorizes what the client will send."""
    return IssueInput(**{**NEW_ISSUE, **overrides}).model_dump(
        mode="json", exclude={"revision"}
    )


class ExecutionFixture(unittest.TestCase):
    """A real database, a real session pin, a real record grant."""

    def setUp(self):
        files_patch = patch.object(env_module, "_env_files", lambda: ())
        files_patch.start()
        self.addCleanup(files_patch.stop)
        env_patch = patch.dict(os.environ, {"LLM_ENABLED": "false", "PIXEL_SYNTHETIC_DEMO": "true",
                                            "PIXEL_DEMO_SEEDS": "true"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db_path = Path(temporary.name) / "execution.sqlite3"
        db_patch = patch.object(db, "DB_PATH", self.db_path)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        db.migrate()
        product_data.seed_if_empty()

        self.directory = OrganizationDirectory()
        self.registry = DefinitionRegistry()
        self.sessions = SessionManager()
        self.ledger = ExecutionLedger()
        self.client = TestClient(app)

        self.directory.add_member(TENANT, OTHER_USER.user_id, "team_member", "planning-team")
        grant_records(TENANT, PRODUCT, USER.user_id, [SCOPE], is_admin=False)
        grant_records(TENANT, PRODUCT, OTHER_USER.user_id, [SCOPE], is_admin=False)
        self.pin = pin_new_session(authorize_product(USER, PRODUCT, self.directory), self.registry)
        self.sessions.ensure_session("s-exec", PRODUCT, USER.user_id, TENANT, SCOPE, pin=self.pin)
        self.definition = self.registry.load(
            self.pin.definition_id, self.pin.definition_version
        ).definition

    # --- helpers ---

    def owner(self, user: AuthUser = USER) -> ExecutionOwner:
        return ExecutionOwner(TENANT, PRODUCT, user.user_id)

    def validated(self, action_key: str, **params) -> ValidatedAction:
        action = GenericAction.for_definition(self.definition, action_key, **params)
        return ValidatedAction(action, self.pin.definition_id, self.pin.definition_version)

    def dispatch_create(self, payload: dict | None = None, user: AuthUser = USER, session_id: str = "s-exec") -> str:
        payload = payload or issue_payload()
        validated = self.validated("create_issue", fields={
            "title": payload["title"], "priority": payload["priority"],
            "assignee": "maya-chen", "project": payload["project"], "status": payload["status"],
        })
        return self.ledger.dispatch(
            validated, self.owner(user), session_id=session_id, turn_id=1,
            write_request=["create_issue", payload],
        ).execution_key

    def dispatch_update(self, issue_id: str, payload: dict, session_id: str = "s-exec") -> str:
        validated = self.validated("update_issue", target=RecordRef("issue", issue_id),
                                   fields={"priority": payload["priority"]})
        return self.ledger.dispatch(
            validated, self.owner(), session_id=session_id, turn_id=1,
            write_request=["update_issue", issue_id, payload],
        ).execution_key

    def post_issue(self, key: str | None, payload: dict, user: AuthUser = USER, session: str | None = "s-exec"):
        headers = {"Authorization": f"Bearer {create_token(user.user_id, user.tenant_id)}"}
        if key:
            headers["X-Execution-Key"] = key
        if session:
            headers["X-Session-Id"] = session
        return self.client.post("/api/demo-data/issues", json=payload, headers=headers)

    def put_issue(self, key: str | None, issue_id: str, payload: dict, user: AuthUser = USER):
        headers = {"Authorization": f"Bearer {create_token(user.user_id, user.tenant_id)}", "X-Session-Id": "s-exec"}
        if key:
            headers["X-Execution-Key"] = key
        return self.client.put(f"/api/demo-data/issues/{issue_id}", json=payload, headers=headers)

    def read(self, statement: str, values: tuple = ()):
        """Read the file directly, outside the application, and close the handle."""
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(statement, values).fetchall()

    def issue_count(self, issue_id: str = "LIN-900") -> int:
        return self.read("select count(*) as n from demo_issues where id = ?", (issue_id,))[0]["n"]

    def execution_rows(self) -> list[sqlite3.Row]:
        return self.read("select * from action_executions")

    def key_row(self, key: str) -> sqlite3.Row:
        rows = self.read("select * from action_executions where execution_key = ?", (key,))
        return rows[0] if rows else None

    def table_counts(self) -> dict[str, int]:
        """Every product-record table, so a refused request can be compared before and after."""
        return {
            table: self.read(f"select count(*) as n from {table}")[0]["n"] for table in RECORD_TABLES
        }

    def clear_records(self) -> None:
        with db.get_connection() as connection:
            connection.executescript(
                "delete from demo_issues; delete from demo_cycles; delete from demo_team_members; "
                "delete from demo_projects; delete from demo_workspace_scopes;"
            )

    def set_expired(self, key: str) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("update action_executions set expires_at = ? where execution_key = ?",
                               (time.time() - 1, key))
            connection.commit()


class KeyedWriteTest(ExecutionFixture):
    def test_a_key_writes_the_record_and_settles_executed(self):
        key = self.dispatch_create()
        response = self.post_issue(key, issue_payload())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["id"], "LIN-900")
        self.assertEqual(self.issue_count(), 1)
        row = self.key_row(key)
        self.assertEqual(row["state"], "executed")
        self.assertIsNotNone(row["settled_at"])
        self.assertEqual((row["result_record_id"], row["result_code"]), ("LIN-900", "created"))

    def test_presenting_the_same_key_again_returns_the_first_outcome(self):
        key = self.dispatch_create()
        first = self.post_issue(key, issue_payload())
        second = self.post_issue(key, issue_payload())
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(self.issue_count(), 1, "a replay must not write a second record")

    def test_the_outcome_survives_a_restart(self):
        """The decision lives in the database, not in this process."""
        key = self.dispatch_create()
        self.post_issue(key, issue_payload())
        self.assertEqual(ExecutionLedger().state_of(key), "executed")
        replayed = self.post_issue(key, issue_payload())
        self.assertEqual(replayed.status_code, 200)
        self.assertEqual(self.issue_count(), 1)

    def test_a_key_cannot_be_used_for_a_different_write(self):
        key = self.dispatch_create()
        response = self.post_issue(key, issue_payload(title="Something else"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "execution_request_mismatch")
        self.assertEqual(self.issue_count(), 0)
        self.assertEqual(self.key_row(key)["state"], "dispatched")

    def test_a_key_cannot_be_used_for_a_different_record(self):
        key = self.dispatch_create()
        response = self.post_issue(key, issue_payload(id="LIN-901"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.issue_count("LIN-901"), 0)

    def test_an_unknown_key_writes_nothing(self):
        response = self.post_issue("not-a-key", issue_payload())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "unknown_execution_key")
        self.assertEqual(self.issue_count(), 0)

    def test_another_callers_key_is_indistinguishable_from_an_unknown_one(self):
        """Whichever check refuses first, the two answers must be identical."""
        key = self.dispatch_create()

        # Presented with someone else's conversation: refused before the key is even examined.
        stolen = self.post_issue(key, issue_payload(), user=OTHER_USER)
        unknown = self.post_issue("not-a-key", issue_payload(), user=OTHER_USER)
        self.assertEqual(stolen.status_code, unknown.status_code)
        self.assertEqual(stolen.json(), unknown.json())

        # Presented inside their own conversation: now the claim is what refuses, and it still
        # says nothing about whose key it is.
        self.sessions.ensure_session("s-theirs", PRODUCT, OTHER_USER.user_id, TENANT, SCOPE, pin=self.pin)
        stolen_own = self.post_issue(key, issue_payload(), user=OTHER_USER, session="s-theirs")
        unknown_own = self.post_issue("not-a-key", issue_payload(), user=OTHER_USER, session="s-theirs")
        self.assertEqual(stolen_own.status_code, 409)
        self.assertEqual(stolen_own.json(), unknown_own.json())
        self.assertEqual(stolen_own.json()["detail"], "unknown_execution_key")

        self.assertEqual(self.issue_count(), 0)
        self.assertEqual(self.key_row(key)["state"], "dispatched")

    def test_an_expired_key_is_refused(self):
        key = self.dispatch_create()
        self.set_expired(key)
        response = self.post_issue(key, issue_payload())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "execution_expired")
        self.assertEqual(self.issue_count(), 0)

    def test_expiry_is_the_platforms_lifetime_not_the_definitions(self):
        key = self.dispatch_create()
        row = self.key_row(key)
        self.assertAlmostEqual(row["expires_at"] - time.time(), KEY_LIFETIME_SECONDS, delta=30)

    def test_a_key_is_only_usable_in_the_conversation_it_was_issued_for(self):
        """A key is not portable between a caller's own sessions."""
        self.sessions.ensure_session("s-other", PRODUCT, USER.user_id, TENANT, SCOPE, pin=self.pin)
        key = self.dispatch_create(session_id="s-other")

        elsewhere = self.post_issue(key, issue_payload(), session="s-exec")
        self.assertEqual(elsewhere.status_code, 409)
        self.assertEqual(elsewhere.json()["detail"], "unknown_execution_key")
        self.assertEqual(self.issue_count(), 0)

        allowed = self.post_issue(key, issue_payload(), session="s-other")
        self.assertEqual(allowed.status_code, 200, allowed.text)
        self.assertEqual(self.issue_count(), 1)

    def test_a_key_needs_a_session_to_be_used_at_all(self):
        """Without the conversation there is no pin to re-check, so the write is refused."""
        key = self.dispatch_create()
        response = self.post_issue(key, issue_payload(), session=None)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "session_not_usable")
        self.assertEqual(self.issue_count(), 0)
        self.assertEqual(self.key_row(key)["state"], "dispatched")

    def test_the_record_owning_product_is_rechecked_inside_the_transaction(self):
        key = self.dispatch_create()
        self.directory.create_team(TENANT, "support-team", "Support")
        self.directory.bind_product(TENANT, "support-desk", "support-team", self.pin.definition_id, 1)
        with db.get_connection() as connection:
            connection.execute("update legacy_record_owner set product_id = ? where singleton = 1",
                               ("support-desk",))
        response = self.post_issue(key, issue_payload())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.issue_count(), 0)


class LedgerDataTest(ExecutionFixture):
    """The ledger keeps identifiers and outcomes, never a copy of the customer's record."""

    def test_no_column_holds_record_content(self):
        key = self.dispatch_create()
        self.post_issue(key, issue_payload())
        row = self.key_row(key)
        stored = " ".join(str(value) for value in tuple(row))
        for content in (NEW_ISSUE["title"], NEW_ISSUE["assignee"], NEW_ISSUE["project"],
                        NEW_ISSUE["priority"]):
            self.assertNotIn(content, stored, f"the ledger must not store {content!r}")
        self.assertEqual(
            sorted(row.keys()),
            sorted([
                "execution_key", "tenant_id", "product_id", "session_id", "turn_id", "user_id",
                "instance_id", "instance_generation",
                "action_key", "capability", "entity", "target_id", "request_digest", "state",
                "result_record_id", "result_code", "reason", "created_at", "expires_at",
                "settled_at",
            ]),
        )

    def test_the_request_is_matched_by_digest_not_by_a_stored_copy(self):
        key = self.dispatch_create()
        digest = self.key_row(key)["request_digest"]
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertNotIn(NEW_ISSUE["title"], digest)

    def test_key_order_in_the_request_body_does_not_cause_a_mismatch(self):
        """The same values in a different order are the same request."""
        payload = issue_payload()
        key = self.dispatch_create(payload)
        reordered = dict(reversed(list(payload.items())))
        self.assertNotEqual(list(reordered), list(payload))
        response = self.post_issue(key, reordered)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.issue_count(), 1)

    def test_a_replay_reloads_the_record_as_it_is_now(self):
        key = self.dispatch_create()
        self.post_issue(key, issue_payload())
        # Someone else changes the ticket after the keyed write committed.
        self.put_issue(None, "LIN-900", issue_payload(title="Renamed by someone else"))
        replay = self.post_issue(key, issue_payload())
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["title"], "Renamed by someone else")
        self.assertEqual(self.issue_count(), 1)

    def test_a_replay_of_a_record_the_caller_can_no_longer_see_is_refused(self):
        key = self.dispatch_create()
        self.assertEqual(self.post_issue(key, issue_payload()).status_code, 200)
        with db.get_connection() as connection:
            connection.execute("update record_grants set scope_ids = ? where user_id = ?",
                               ('["workspace-platform"]', USER.user_id))
        replay = self.post_issue(key, issue_payload())
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json()["detail"], "execution_result_unavailable")

    def test_a_settled_key_is_one_historical_attempt_not_a_retry_loop(self):
        """The requested semantic: a failed key keeps failing; a new attempt needs a new key."""
        missing = issue_payload(id="LIN-404", title="Missing", priority="Low")
        key = self.dispatch_update("LIN-404", missing)
        first = self.put_issue(key, "LIN-404", missing)
        self.assertEqual(first.status_code, 404)
        self.assertEqual(self.key_row(key)["reason"], "record_not_found")

        # The data now allows what the action asked for.
        self.post_issue(None, issue_payload(id="LIN-404", title="Missing", priority="High"))
        self.assertEqual(self.issue_count("LIN-404"), 1)

        # The same key still reports the original failure, and changes nothing.
        again = self.put_issue(key, "LIN-404", missing)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["detail"], "record_not_found")
        self.assertEqual(
            self.read("select priority from demo_issues where id = ?", ("LIN-404",))[0]["priority"],
            "High", "a settled key must not change the record",
        )

        # A new key is a new attempt, and it succeeds.
        fresh = self.dispatch_update("LIN-404", missing)
        retried = self.put_issue(fresh, "LIN-404", missing)
        self.assertEqual(retried.status_code, 200, retried.text)
        self.assertEqual(
            self.read("select priority from demo_issues where id = ?", ("LIN-404",))[0]["priority"],
            "Low",
        )


class ReviewFindingTest(ExecutionFixture):
    """Regressions for the defects the slice 3 review found."""

    def test_a_keyed_write_never_creates_reference_data(self):
        """Seeding is the deployment bootstrap's job, so a request cannot conjure workspaces.

        With the workspaces gone, the write is refused by the scope rule and settles as failed:
        the data genuinely is not there. What must not happen is the request repopulating it.
        """
        self.clear_records()
        key = self.dispatch_create()
        response = self.post_issue(key, issue_payload())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.key_row(key)["reason"], "scope_violation")
        self.assertEqual(self.table_counts(), dict.fromkeys(RECORD_TABLES, 0))

    def test_application_startup_is_what_seeds_records(self):
        """The deployment creates demo data, so no request ever has to."""
        self.clear_records()
        with TestClient(app):  # runs the startup hook
            pass
        self.assertGreater(self.table_counts()["demo_projects"], 0)

    def test_a_session_belonging_to_another_caller_cannot_be_used(self):
        self.sessions.ensure_session("s-theirs", PRODUCT, OTHER_USER.user_id, TENANT, SCOPE, pin=self.pin)
        key = self.dispatch_create(session_id="s-theirs")
        response = self.post_issue(key, issue_payload(), session="s-theirs")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "session_not_usable")
        self.assertEqual(self.issue_count(), 0)

    def test_the_store_refuses_a_connection_with_no_open_transaction(self):
        """Otherwise the record change and the outcome would commit separately, unnoticed."""
        with db.get_connection() as connection:
            with self.assertRaises(RuntimeError):
                product_data.save_issue(issue_payload(), None, connection=connection)

    def test_the_last_executed_action_is_the_most_recent_one(self):
        first = self.dispatch_create()
        self.post_issue(first, issue_payload())
        second = self.ledger.dispatch(
            self.validated("update_issue", target=RecordRef("issue", "LIN-900"),
                           fields={"priority": "Low"}),
            self.owner(), session_id="s-exec", turn_id=2,
            write_request=["update_issue", "LIN-900", issue_payload(priority="Low")],
        ).execution_key
        self.put_issue(second, "LIN-900", issue_payload(priority="Low"))
        latest = self.ledger.last_executed("s-exec")
        self.assertEqual(latest.action_key, "update_issue", "settled in the same second, but later")
        self.assertEqual((latest.record_id, latest.result_code), ("LIN-900", "updated"))
        self.assertFalse(hasattr(latest, "fields"), "the ledger does not remember field values")


class CancellationRaceTest(ExecutionFixture):
    def test_cancelling_before_the_write_refuses_it(self):
        key = self.dispatch_create()
        self.assertEqual(self.ledger.cancel_turn("s-exec", 1), 1)
        response = self.post_issue(key, issue_payload())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "execution_cancelled")
        self.assertEqual(self.issue_count(), 0)

    def test_cancelling_after_the_write_does_not_undo_or_relabel_it(self):
        key = self.dispatch_create()
        self.assertEqual(self.post_issue(key, issue_payload()).status_code, 200)
        self.assertEqual(self.ledger.cancel_turn("s-exec", 1), 0, "a settled key is never cancelled")
        self.assertEqual(self.key_row(key)["state"], "executed")
        self.assertEqual(self.issue_count(), 1)

    def test_cancelling_one_turn_leaves_another_turns_key_alone(self):
        key = self.dispatch_create()
        other = self.ledger.dispatch(
            self.validated("create_issue", fields={"title": "Second", "priority": "Low",
                                                   "assignee": "maya-chen", "project": "Integrations",
                                                   "status": "Todo"}),
            self.owner(), session_id="s-exec", turn_id=2,
            write_request=["create_issue", issue_payload(id="LIN-901", title="Second", priority="Low")],
        ).execution_key
        self.ledger.cancel_turn("s-exec", 1)
        self.assertEqual(self.key_row(key)["state"], "cancelled")
        self.assertEqual(self.key_row(other)["state"], "dispatched")


class ConcurrencyTest(ExecutionFixture):
    def test_two_callers_racing_one_key_produce_one_record(self):
        key = self.dispatch_create()
        results: list[int] = []
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait()
            results.append(self.post_issue(key, issue_payload()).status_code)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(self.issue_count(), 1, "the record must be written exactly once")
        self.assertEqual(sorted(results), [200, 200], "the loser replays the winner's outcome")
        self.assertEqual(self.key_row(key)["state"], "executed")

    def test_a_write_racing_a_cancellation_ends_in_one_state(self):
        key = self.dispatch_create()
        outcome: dict[str, int] = {}
        barrier = threading.Barrier(2)

        def write():
            barrier.wait()
            outcome["status"] = self.post_issue(key, issue_payload()).status_code

        def cancel():
            barrier.wait()
            outcome["cancelled"] = self.ledger.cancel_turn("s-exec", 1)

        threads = [threading.Thread(target=write), threading.Thread(target=cancel)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        state = self.key_row(key)["state"]
        self.assertIn(state, ("executed", "cancelled"))
        if state == "executed":
            self.assertEqual((outcome["status"], self.issue_count()), (200, 1))
        else:
            self.assertEqual((outcome["status"], self.issue_count()), (409, 0))


class WriteLockTest(ExecutionFixture):
    def test_no_one_else_can_write_while_a_keyed_write_is_in_flight(self):
        """The boundary is a database write lock, not merely ordered Python.

        While the keyed write holds `begin immediate`, another connection cannot start a write
        at all, so the authority the guard read cannot change underneath the record change.
        """
        key = self.dispatch_create()
        blocked: list[str] = []
        real_save = product_data.save_issue

        def save_while_probing(*args, **kwargs):
            outsider = sqlite3.connect(self.db_path, timeout=0.2)
            try:
                outsider.execute("begin immediate")
                outsider.execute("update products set state = 'disabled' where product_id = ?", (PRODUCT,))
                outsider.commit()
                blocked.append("changed")
            except sqlite3.OperationalError as error:
                blocked.append(str(error))
            finally:
                outsider.close()
            return real_save(*args, **kwargs)

        with patch.object(product_data, "save_issue", side_effect=save_while_probing):
            response = self.post_issue(key, issue_payload())

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(blocked), 1)
        self.assertIn("locked", blocked[0], f"an outside writer must be locked out, got {blocked[0]}")
        self.assertEqual(self.key_row(key)["state"], "executed")


class RollbackTest(ExecutionFixture):
    def test_a_rule_rejection_commits_as_failed_and_changes_nothing(self):
        key = self.dispatch_update("LIN-404", issue_payload(id="LIN-404", title="Missing", priority="Low"))
        response = self.put_issue(key, "LIN-404", issue_payload(id="LIN-404", title="Missing",
                                                                priority="Low"))
        self.assertEqual(response.status_code, 404)
        row = self.key_row(key)
        self.assertEqual((row["state"], row["reason"]), ("failed", "record_not_found"))

    def test_replaying_a_failed_key_repeats_the_failure_and_never_writes(self):
        key = self.dispatch_update("LIN-404", issue_payload(id="LIN-404", title="Missing", priority="Low"))
        payload = issue_payload(id="LIN-404", title="Missing", priority="Low")
        self.put_issue(key, "LIN-404", payload)
        replay = self.put_issue(key, "LIN-404", payload)
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(replay.json()["detail"], "record_not_found")
        self.assertEqual(self.issue_count("LIN-404"), 0)

    def test_an_unexpected_error_rolls_the_whole_transaction_back(self):
        """No record, no outcome: the key stays dispatched, which is the only retryable state."""
        key = self.dispatch_create()
        with patch.object(product_data, "save_issue", side_effect=RuntimeError("disk on fire")):
            with self.assertRaises(RuntimeError):
                self.post_issue(key, issue_payload())
        self.assertEqual(self.issue_count(), 0)
        self.assertEqual(self.key_row(key)["state"], "dispatched")

    def test_after_a_rolled_back_attempt_the_same_key_still_works(self):
        key = self.dispatch_create()
        with patch.object(product_data, "save_issue", side_effect=RuntimeError("disk on fire")):
            with self.assertRaises(RuntimeError):
                self.post_issue(key, issue_payload())
        response = self.post_issue(key, issue_payload())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.issue_count(), 1)


class RecheckTest(ExecutionFixture):
    """Everything checked when the action was proposed is checked again at the write."""

    def assert_refused_without_writing(self, key: str, detail: str, status: int = 403):
        response = self.post_issue(key, issue_payload())
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["detail"], detail)
        self.assertEqual(self.issue_count(), 0)
        self.assertEqual(self.key_row(key)["state"], "dispatched")

    def test_a_product_disabled_after_dispatch_refuses_the_write(self):
        key = self.dispatch_create()
        self.directory.set_product_state(TENANT, PRODUCT, "disabled")
        self.assert_refused_without_writing(key, "access_product_disabled")

    def test_an_organization_suspended_after_dispatch_refuses_the_write(self):
        """Refused by authentication before the handler; the guard would refuse it too."""
        key = self.dispatch_create()
        self.directory.set_organization_state(TENANT, "suspended")
        response = self.post_issue(key, issue_payload())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.issue_count(), 0)
        self.assertEqual(self.key_row(key)["state"], "dispatched")

    def test_a_team_disabled_after_dispatch_refuses_the_write(self):
        key = self.dispatch_create()
        self.directory.set_team_state(TENANT, "planning-team", "disabled")
        self.assert_refused_without_writing(key, "access_team_disabled")

    def test_a_definition_revoked_after_dispatch_refuses_the_write(self):
        key = self.dispatch_create()
        self.registry.revoke(self.pin.definition_id, self.pin.definition_version)
        self.assert_refused_without_writing(key, "session_definition_revoked")

    def test_a_withdrawn_record_grant_refuses_the_write(self):
        key = self.dispatch_create()
        with db.get_connection() as connection:
            connection.execute("delete from record_grants where user_id = ?", (USER.user_id,))
        self.assertIsNone(record_grant(TENANT, PRODUCT, USER.user_id))
        self.assert_refused_without_writing(key, "record_access_withdrawn")

    def test_a_write_outside_the_callers_workspace_is_rejected_by_the_rule_not_the_key(self):
        payload = issue_payload(project="Integrations")
        key = self.dispatch_create(payload)
        with db.get_connection() as connection:
            connection.execute("update record_grants set scope_ids = ? where user_id = ?",
                               ('["workspace-support"]', USER.user_id))
        response = self.post_issue(key, payload)
        self.assertIn(response.status_code, (403,))
        self.assertEqual(self.issue_count(), 0)


class KeylessWriteTest(ExecutionFixture):
    """Writes without a key are unchanged, and are not covered by the execution guarantee."""

    def test_a_write_without_a_key_still_works_and_records_nothing(self):
        response = self.post_issue(None, issue_payload(), session=None)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.issue_count(), 1)
        self.assertEqual(self.execution_rows(), [], "a keyless write must not invent an execution row")

    def test_a_keyless_write_is_still_scope_checked(self):
        with db.get_connection() as connection:
            connection.execute("update record_grants set scope_ids = ? where user_id = ?",
                               ('["workspace-support"]', USER.user_id))
        response = self.post_issue(None, issue_payload(), session=None)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.issue_count(), 0)


class TransactionReviewTest(ExecutionFixture):
    """The two defects the second transaction review reproduced.

    Both were about a keyed write doing something other than the one change it authorized: one
    settling as executed without creating anything, and one changing data while being refused.
    """

    def test_one_approved_create_produces_exactly_one_new_record(self):
        before = self.table_counts()["demo_issues"]
        key = self.dispatch_create()
        response = self.post_issue(key, issue_payload())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.table_counts()["demo_issues"], before + 1)
        self.assertEqual(self.key_row(key)["state"], "executed")
        self.assertEqual(self.issue_count(), 1)

    def test_a_keyed_write_refuses_the_legacy_retry_header(self):
        """Two idempotency mechanisms could report an older receipt as this action's outcome."""
        key = self.dispatch_create()
        headers = {
            "Authorization": f"Bearer {create_token(USER.user_id, USER.tenant_id)}",
            "X-Execution-Key": key, "X-Session-Id": "s-exec", "Idempotency-Key": "legacy-1",
        }
        response = self.client.post("/api/demo-data/issues", json=issue_payload(), headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.issue_count(), 0)
        self.assertEqual(self.key_row(key)["state"], "dispatched")

    def test_a_keyed_create_cannot_settle_on_an_older_receipt(self):
        """The reproduction: a manual create, then the same action keyed with the same header."""
        shared = "shared-receipt"
        manual = self.client.post(
            "/api/demo-data/issues", json=issue_payload(),
            headers={"Authorization": f"Bearer {create_token(USER.user_id, USER.tenant_id)}",
                     "Idempotency-Key": shared},
        )
        self.assertEqual(manual.status_code, 200, manual.text)
        self.assertEqual(self.issue_count(), 1)

        key = self.dispatch_create()
        keyed = self.client.post(
            "/api/demo-data/issues", json=issue_payload(),
            headers={"Authorization": f"Bearer {create_token(USER.user_id, USER.tenant_id)}",
                     "Idempotency-Key": shared, "X-Execution-Key": key, "X-Session-Id": "s-exec"},
        )
        self.assertNotEqual(keyed.status_code, 200, "this must not report a create that did not happen")
        self.assertNotEqual(self.key_row(key)["state"], "executed")

    def test_a_keyed_write_without_the_legacy_header_is_unaffected(self):
        key = self.dispatch_create()
        self.assertEqual(self.post_issue(key, issue_payload()).status_code, 200)
        self.assertEqual(self.issue_count(), 1)

    def test_a_keyless_write_may_still_use_the_legacy_header(self):
        """The old mechanism is untouched where it is the only one."""
        headers = {"Authorization": f"Bearer {create_token(USER.user_id, USER.tenant_id)}",
                   "Idempotency-Key": "legacy-2"}
        first = self.client.post("/api/demo-data/issues", json=issue_payload(), headers=headers)
        second = self.client.post("/api/demo-data/issues", json=issue_payload(), headers=headers)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.issue_count(), 1)

    def test_a_refused_request_leaves_every_record_table_untouched(self):
        """An unknown key, a foreign key, a missing session and a lost grant: none may mutate."""
        valid = self.dispatch_create()
        self.sessions.ensure_session("s-theirs", PRODUCT, OTHER_USER.user_id, TENANT, SCOPE, pin=self.pin)
        attempts = {
            "unknown key": lambda: self.post_issue("not-a-key", issue_payload()),
            "another caller's key": lambda: self.post_issue(valid, issue_payload(), user=OTHER_USER,
                                                            session="s-theirs"),
            "no session": lambda: self.post_issue(valid, issue_payload(), session=None),
            "legacy header as well": lambda: self.client.post(
                "/api/demo-data/issues", json=issue_payload(),
                headers={"Authorization": f"Bearer {create_token(USER.user_id, USER.tenant_id)}",
                         "X-Execution-Key": valid, "X-Session-Id": "s-exec",
                         "Idempotency-Key": "legacy-3"}),
        }
        for label, attempt in attempts.items():
            with self.subTest(label):
                before = self.table_counts()
                response = attempt()
                self.assertNotEqual(response.status_code, 200, response.text)
                self.assertEqual(self.table_counts(), before, "a refused request changed product data")

    def test_a_refused_request_on_an_empty_store_creates_nothing(self):
        """The reproduction: a rejected request used to repopulate every table."""
        self.clear_records()
        before = self.table_counts()
        response = self.post_issue("not-a-key", issue_payload())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.table_counts(), before)
        self.assertEqual(before, dict.fromkeys(RECORD_TABLES, 0))


if __name__ == "__main__":
    unittest.main()
