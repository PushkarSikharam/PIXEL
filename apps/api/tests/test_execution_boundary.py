"""The execution boundary: one transaction, one write, one outcome (3.2 plan, section 5).

These tests exist because the dangerous part of the generic engine is not deciding what to do,
it is doing it exactly once. Each test drives the real HTTP endpoint against a real SQLite file,
because the guarantee is a database guarantee, not a Python one.

5b re-points them to the change-set contract (5b plan, sections 7 and 8): a key binds the exact
change, the keyed endpoints carry only that change, every recognized outcome returns a receipt, and
an unrecognized key is a plain 404.

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

NEW_FIELDS = {"title": "Keyed write", "priority": "High", "assignee": "Maya Chen",
              "project": "Integrations", "status": "Todo"}
FORM_ISSUE = {"id": "LIN-900", "title": "Form write", "priority": "High", "assignee": "Maya Chen",
              "project": "Integrations", "status": "Todo"}


def create_fields(**overrides) -> dict:
    return {**NEW_FIELDS, **overrides}


def create_change(fields: dict) -> dict:
    return {"action": "create_issue", "fields": fields}


def update_change(issue_id: str, changes: dict) -> dict:
    return {"action": "update_issue", "target": issue_id, "changes": changes}


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

    def dispatch_create(self, fields: dict | None = None, user: AuthUser = USER, session_id: str = "s-exec",
                        turn_id: int = 1) -> str:
        fields = fields or create_fields()
        validated = self.validated("create_issue", fields={
            "title": fields["title"], "priority": fields["priority"],
            "assignee": "maya-chen", "project": fields["project"], "status": fields["status"],
        })
        return self.ledger.dispatch(
            validated, self.owner(user), session_id=session_id, turn_id=turn_id, scope_id=SCOPE,
            change_set=create_change(fields),
        ).execution_key

    def dispatch_update(self, issue_id: str, changes: dict, session_id: str = "s-exec", turn_id: int = 1) -> str:
        validated = self.validated("update_issue", target=RecordRef("issue", issue_id),
                                   fields={"priority": changes.get("priority", "Low")})
        return self.ledger.dispatch(
            validated, self.owner(), session_id=session_id, turn_id=turn_id, scope_id=SCOPE,
            change_set=update_change(issue_id, changes),
        ).execution_key

    def headers(self, key: str | None, user: AuthUser = USER, session: str | None = "s-exec") -> dict:
        headers = {"Authorization": f"Bearer {create_token(user.user_id, user.tenant_id)}"}
        if key:
            headers["X-Execution-Key"] = key
        if session:
            headers["X-Session-Id"] = session
        return headers

    def post_keyed(self, key: str, fields: dict | None = None, user: AuthUser = USER,
                   session: str | None = "s-exec"):
        return self.client.post("/api/demo-data/issues", json={"fields": fields or create_fields()},
                                headers=self.headers(key, user, session))

    def patch_keyed(self, key: str, issue_id: str, changes: dict, user: AuthUser = USER,
                    session: str | None = "s-exec"):
        return self.client.patch(f"/api/demo-data/issues/{issue_id}", json={"changes": changes},
                                 headers=self.headers(key, user, session))

    def post_form(self, issue: dict, **headers):
        return self.client.post("/api/demo-data/issues", json=issue,
                                headers={**self.headers(None, session=None), **headers})

    def put_form(self, issue_id: str, issue: dict):
        return self.client.put(f"/api/demo-data/issues/{issue_id}", json=issue,
                               headers=self.headers(None, session=None))

    def read(self, statement: str, values: tuple = ()):
        """Read the file directly, outside the application, and close the handle."""
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(statement, values).fetchall()

    def keyed_count(self, title: str = "Keyed write") -> int:
        return self.read("select count(*) as n from demo_issues where title = ?", (title,))[0]["n"]

    def issue_count(self, issue_id: str) -> int:
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

    def cancel_keys(self, turn_id: int) -> int:
        with db.get_connection() as connection:
            connection.execute("begin immediate")
            return self.ledger.cancel_turn(connection, self.owner(), "s-exec", turn_id)

    def assert_receipt(self, response, status: int, outcome: str, code: str) -> dict:
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertEqual((body["outcome"], body["code"]), (outcome, code))
        return body


class KeyedWriteTest(ExecutionFixture):
    def test_a_key_writes_the_record_and_settles_executed(self):
        key = self.dispatch_create()
        body = self.assert_receipt(self.post_keyed(key), 200, "executed", "applied")
        record_id = body["record"]["id"]
        self.assertTrue(body["speech"].startswith(f"Created {record_id}:"), body["speech"])
        self.assertEqual(self.keyed_count(), 1)
        row = self.key_row(key)
        self.assertEqual((row["state"], row["result_record_id"], row["result_code"]),
                         ("executed", record_id, "applied"))
        self.assertIsNotNone(row["settled_at"])

    def test_presenting_the_same_key_again_returns_the_first_outcome(self):
        key = self.dispatch_create()
        first = self.assert_receipt(self.post_keyed(key), 200, "executed", "applied")
        second = self.assert_receipt(self.post_keyed(key), 200, "executed", "applied")
        self.assertEqual(second["record"]["id"], first["record"]["id"])
        self.assertEqual(second["speech"], "This change was already applied.")
        self.assertEqual(self.keyed_count(), 1, "a replay must not write a second record")

    def test_the_outcome_survives_a_restart(self):
        """The decision lives in the database, not in this process."""
        key = self.dispatch_create()
        self.post_keyed(key)
        self.assertEqual(ExecutionLedger().state_of(key), "executed")
        self.assert_receipt(self.post_keyed(key), 200, "executed", "applied")
        self.assertEqual(self.keyed_count(), 1)

    def test_a_key_cannot_be_used_for_a_different_change(self):
        key = self.dispatch_create()
        self.assert_receipt(self.post_keyed(key, create_fields(title="Something else")), 409, "failed",
                            "invalid_change")
        self.assertEqual(self.keyed_count("Something else"), 0)
        self.assertEqual((self.key_row(key)["state"], self.key_row(key)["result_code"]),
                         ("failed", "invalid_change"))
        # The owned key cannot be probed again or reused with the right change afterwards.
        self.assert_receipt(self.post_keyed(key), 409, "failed", "invalid_change")
        self.assertEqual(self.keyed_count(), 0)

    def test_an_unknown_key_writes_nothing(self):
        response = self.post_keyed("not-a-key")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("outcome", response.json())
        self.assertEqual(self.keyed_count(), 0)

    def test_another_callers_key_is_indistinguishable_from_an_unknown_one(self):
        """Whichever check refuses first, the two answers must be identical."""
        key = self.dispatch_create()

        # Presented with someone else's conversation: refused before the key is even examined.
        stolen = self.post_keyed(key, user=OTHER_USER)
        unknown = self.post_keyed("not-a-key", user=OTHER_USER)
        self.assertEqual(stolen.status_code, unknown.status_code)
        self.assertEqual(stolen.json(), unknown.json())

        # Presented inside their own conversation: now the claim is what refuses, and it still
        # says nothing about whose key it is.
        self.sessions.ensure_session("s-theirs", PRODUCT, OTHER_USER.user_id, TENANT, SCOPE, pin=self.pin)
        stolen_own = self.post_keyed(key, user=OTHER_USER, session="s-theirs")
        unknown_own = self.post_keyed("not-a-key", user=OTHER_USER, session="s-theirs")
        self.assertEqual(stolen_own.status_code, 404)
        self.assertEqual(stolen_own.json(), unknown_own.json())

        self.assertEqual(self.keyed_count(), 0)
        self.assertEqual(self.key_row(key)["state"], "dispatched")

    def test_an_expired_key_is_refused_and_the_expiry_is_durable(self):
        key = self.dispatch_create()
        self.set_expired(key)
        body = self.assert_receipt(self.post_keyed(key), 409, "refused", "expired")
        self.assertEqual(body["speech"], "This change expired before it was applied.")
        self.assertEqual((self.key_row(key)["state"], self.key_row(key)["result_code"]), ("cancelled", "expired"))
        self.assertEqual(self.post_keyed(key).json(), body, "the replay is identical")
        self.assertEqual(self.keyed_count(), 0)

    def test_expiry_is_the_platforms_lifetime_not_the_definitions(self):
        key = self.dispatch_create()
        row = self.key_row(key)
        self.assertAlmostEqual(row["expires_at"] - time.time(), KEY_LIFETIME_SECONDS, delta=30)

    def test_a_key_is_only_usable_in_the_conversation_it_was_issued_for(self):
        """A key is not portable between a caller's own sessions."""
        self.sessions.ensure_session("s-other", PRODUCT, USER.user_id, TENANT, SCOPE, pin=self.pin)
        key = self.dispatch_create(session_id="s-other")
        self.assertEqual(self.post_keyed(key, session="s-exec").status_code, 404)
        self.assertEqual(self.keyed_count(), 0)
        self.assert_receipt(self.post_keyed(key, session="s-other"), 200, "executed", "applied")
        self.assertEqual(self.keyed_count(), 1)

    def test_a_key_needs_its_session_to_be_recognized(self):
        key = self.dispatch_create()
        self.assertEqual(self.post_keyed(key, session=None).status_code, 404)
        self.assertEqual(self.keyed_count(), 0)
        self.assertEqual(self.key_row(key)["state"], "dispatched")

    def test_the_record_owning_product_is_rechecked_inside_the_transaction(self):
        key = self.dispatch_create()
        self.directory.create_team(TENANT, "support-team", "Support")
        self.directory.bind_product(TENANT, "support-desk", "support-team", self.pin.definition_id, 1)
        with db.get_connection() as connection:
            connection.execute("update legacy_record_owner set product_id = ? where singleton = 1",
                               ("support-desk",))
        self.assertEqual(self.post_keyed(key).status_code, 403)
        self.assertEqual(self.keyed_count(), 0)

    def test_a_whole_ticket_put_cannot_carry_a_key(self):
        key = self.dispatch_update("PIX-1", {"priority": "Low"})
        response = self.client.put("/api/demo-data/issues/PIX-1", json=FORM_ISSUE,
                                   headers=self.headers(key))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.key_row(key)["state"], "dispatched")


class LedgerDataTest(ExecutionFixture):
    """The ledger keeps identifiers and outcomes, never a copy of the customer's record."""

    def test_no_column_holds_record_content(self):
        key = self.dispatch_create()
        self.post_keyed(key)
        row = self.key_row(key)
        stored = " ".join(str(value) for value in tuple(row))
        for content in (NEW_FIELDS["title"], NEW_FIELDS["assignee"], NEW_FIELDS["project"],
                        NEW_FIELDS["priority"], "Created"):
            self.assertNotIn(content, stored, f"the ledger must not store {content!r}")
        self.assertEqual(
            sorted(row.keys()),
            sorted([
                "execution_key", "tenant_id", "product_id", "session_id", "turn_id", "user_id",
                "instance_id", "instance_generation", "scope_id",
                "action_key", "capability", "entity", "target_id", "request_digest", "state",
                "result_record_id", "result_code", "reason", "created_at", "expires_at",
                "settled_at",
            ]),
        )

    def test_the_change_is_matched_by_digest_not_by_a_stored_copy(self):
        key = self.dispatch_create()
        digest = self.key_row(key)["request_digest"]
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertNotIn(NEW_FIELDS["title"], digest)

    def test_key_order_in_the_change_does_not_cause_a_mismatch(self):
        fields = create_fields()
        key = self.dispatch_create(fields)
        reordered = dict(reversed(list(fields.items())))
        self.assertNotEqual(list(reordered), list(fields))
        self.assert_receipt(self.post_keyed(key, reordered), 200, "executed", "applied")
        self.assertEqual(self.keyed_count(), 1)

    def test_a_replay_reloads_the_record_as_it_is_now(self):
        key = self.dispatch_create()
        record = self.assert_receipt(self.post_keyed(key), 200, "executed", "applied")["record"]
        # Someone else changes the ticket after the keyed write committed.
        renamed = {name: record[name] for name in ("id", "title", "priority", "assignee", "project",
                                                   "projectId", "status")}
        self.assertEqual(self.put_form(record["id"], {**renamed, "title": "Renamed by someone else"}).status_code,
                         200)
        replay = self.assert_receipt(self.post_keyed(key), 200, "executed", "applied")
        self.assertEqual(replay["record"]["title"], "Renamed by someone else")

    def test_a_replay_of_a_record_the_caller_can_no_longer_see_names_no_content(self):
        key = self.dispatch_create()
        self.assert_receipt(self.post_keyed(key), 200, "executed", "applied")
        with db.get_connection() as connection:
            connection.execute("update record_grants set scope_ids = ? where user_id = ?",
                               ('["workspace-platform"]', USER.user_id))
        body = self.assert_receipt(self.post_keyed(key), 200, "executed", "execution_result_unavailable")
        self.assertIsNone(body["record"])
        self.assertEqual(body["speech"],
                         "This change was already applied, and that record is no longer available here.")

    def test_a_settled_key_is_one_historical_attempt_not_a_retry_loop(self):
        """The requested semantic: a failed key keeps failing; a new attempt needs a new key."""
        key = self.dispatch_update("LIN-404", {"priority": "Low"})
        self.assert_receipt(self.patch_keyed(key, "LIN-404", {"priority": "Low"}), 409, "failed",
                            "record_not_found")
        self.assertEqual(self.key_row(key)["result_code"], "record_not_found")

        # The data now allows what the action asked for.
        self.assertEqual(self.post_form({**FORM_ISSUE, "id": "LIN-404"}).status_code, 200)

        # The same key still reports the original failure, and changes nothing.
        self.assert_receipt(self.patch_keyed(key, "LIN-404", {"priority": "Low"}), 409, "failed",
                            "record_not_found")
        self.assertEqual(
            self.read("select priority from demo_issues where id = ?", ("LIN-404",))[0]["priority"],
            "High", "a settled key must not change the record",
        )

        # A new key is a new attempt, and it succeeds.
        fresh = self.dispatch_update("LIN-404", {"priority": "Low"}, turn_id=2)
        body = self.assert_receipt(self.patch_keyed(fresh, "LIN-404", {"priority": "Low"}), 200, "executed",
                                   "applied")
        self.assertEqual(body["speech"], "Updated LIN-404: priority to Low.")
        self.assertEqual(
            self.read("select priority from demo_issues where id = ?", ("LIN-404",))[0]["priority"],
            "Low",
        )


class ReviewFindingTest(ExecutionFixture):
    """Regressions for the defects the slice 3 review found."""

    def test_a_keyed_write_never_creates_reference_data(self):
        """Seeding is the deployment bootstrap's job, so a request cannot conjure workspaces."""
        self.clear_records()
        key = self.dispatch_create()
        self.assert_receipt(self.post_keyed(key), 409, "failed", "invalid_change")
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
        response = self.post_keyed(key, session="s-theirs")
        self.assertEqual(response.status_code, 404, "another caller's session is simply not found")
        self.assertEqual(response.json(), self.post_keyed("not-a-key", session="s-theirs").json())
        self.assertEqual(self.keyed_count(), 0)

    def test_the_store_refuses_a_connection_with_no_open_transaction(self):
        """Otherwise the record change and the outcome would commit separately, unnoticed."""
        with db.get_connection() as connection:
            with self.assertRaises(RuntimeError):
                product_data.save_issue(dict(FORM_ISSUE), None, connection=connection)

    def test_the_last_executed_action_is_the_most_recent_one(self):
        first = self.dispatch_create()
        record_id = self.post_keyed(first).json()["record"]["id"]
        second = self.dispatch_update(record_id, {"priority": "Low"}, turn_id=2)
        self.assert_receipt(self.patch_keyed(second, record_id, {"priority": "Low"}), 200, "executed", "applied")
        latest = self.ledger.last_executed(self.owner(), "s-exec", SCOPE)
        self.assertEqual(latest.action_key, "update_issue", "settled in the same second, but later")
        self.assertEqual((latest.record_id, latest.result_code), (record_id, "applied"))
        self.assertFalse(hasattr(latest, "fields"), "the ledger does not remember field values")


class CancellationRaceTest(ExecutionFixture):
    def test_cancelling_before_the_write_refuses_it(self):
        key = self.dispatch_create()
        self.assertEqual(self.cancel_keys(1), 1)
        body = self.assert_receipt(self.post_keyed(key), 409, "refused", "user_cancelled")
        self.assertEqual(body["speech"], "This change was cancelled before it was applied.")
        self.assertEqual(self.keyed_count(), 0)

    def test_cancelling_after_the_write_does_not_undo_or_relabel_it(self):
        key = self.dispatch_create()
        self.assert_receipt(self.post_keyed(key), 200, "executed", "applied")
        self.assertEqual(self.cancel_keys(1), 0, "a settled key is never cancelled")
        self.assertEqual(self.key_row(key)["state"], "executed")
        self.assertEqual(self.keyed_count(), 1)

    def test_cancelling_one_turn_leaves_another_turns_key_alone(self):
        key = self.dispatch_create()
        other = self.dispatch_create(create_fields(title="Second", priority="Low"), turn_id=2)
        self.cancel_keys(1)
        self.assertEqual(self.key_row(key)["state"], "cancelled")
        self.assertEqual(self.key_row(other)["state"], "dispatched")


class ConcurrencyTest(ExecutionFixture):
    def test_two_callers_racing_one_key_produce_one_record(self):
        key = self.dispatch_create()
        results: list[int] = []
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait()
            results.append(self.post_keyed(key).status_code)

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(self.keyed_count(), 1, "the record must be written exactly once")
        self.assertEqual(sorted(results), [200, 200], "the loser replays the winner's outcome")
        self.assertEqual(self.key_row(key)["state"], "executed")

    def test_a_write_racing_a_cancellation_ends_in_one_state(self):
        key = self.dispatch_create()
        outcome: dict[str, int] = {}
        barrier = threading.Barrier(2)

        def write():
            barrier.wait()
            outcome["status"] = self.post_keyed(key).status_code

        def cancel():
            barrier.wait()
            outcome["cancelled"] = self.cancel_keys(1)

        threads = [threading.Thread(target=write), threading.Thread(target=cancel)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        state = self.key_row(key)["state"]
        self.assertIn(state, ("executed", "cancelled"))
        if state == "executed":
            self.assertEqual((outcome["status"], self.keyed_count()), (200, 1))
        else:
            self.assertEqual((outcome["status"], self.keyed_count()), (409, 0))


class WriteLockTest(ExecutionFixture):
    def test_no_one_else_can_write_while_a_keyed_write_is_in_flight(self):
        """The boundary is a database write lock, not merely ordered Python."""
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
            response = self.post_keyed(key)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(blocked), 1)
        self.assertIn("locked", blocked[0], f"an outside writer must be locked out, got {blocked[0]}")
        self.assertEqual(self.key_row(key)["state"], "executed")


class RollbackTest(ExecutionFixture):
    def test_a_rule_rejection_commits_as_failed_and_changes_nothing(self):
        key = self.dispatch_update("LIN-404", {"priority": "Low"})
        self.assert_receipt(self.patch_keyed(key, "LIN-404", {"priority": "Low"}), 409, "failed",
                            "record_not_found")
        row = self.key_row(key)
        self.assertEqual((row["state"], row["result_code"]), ("failed", "record_not_found"))

    def test_replaying_a_failed_key_repeats_the_failure_and_never_writes(self):
        key = self.dispatch_update("LIN-404", {"priority": "Low"})
        first = self.patch_keyed(key, "LIN-404", {"priority": "Low"})
        replay = self.patch_keyed(key, "LIN-404", {"priority": "Low"})
        self.assertEqual((replay.status_code, replay.json()), (first.status_code, first.json()))
        self.assertEqual(self.issue_count("LIN-404"), 0)

    def test_an_unexpected_error_rolls_the_whole_transaction_back(self):
        """No record, no outcome: the key stays dispatched, which is the only retryable state."""
        key = self.dispatch_create()
        with patch.object(product_data, "save_issue", side_effect=RuntimeError("disk on fire")):
            with self.assertRaises(RuntimeError):
                self.post_keyed(key)
        self.assertEqual(self.keyed_count(), 0)
        self.assertEqual(self.key_row(key)["state"], "dispatched")

    def test_after_a_rolled_back_attempt_the_same_key_still_works(self):
        key = self.dispatch_create()
        with patch.object(product_data, "save_issue", side_effect=RuntimeError("disk on fire")):
            with self.assertRaises(RuntimeError):
                self.post_keyed(key)
        self.assert_receipt(self.post_keyed(key), 200, "executed", "applied")
        self.assertEqual(self.keyed_count(), 1)


class RecheckTest(ExecutionFixture):
    """Everything checked when the action was proposed is checked again at the write."""

    def assert_refused_without_writing(self, key: str, detail: str, status: int = 403):
        response = self.post_keyed(key)
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["detail"], detail)
        self.assertEqual(self.keyed_count(), 0)
        self.assertEqual(self.key_row(key)["state"], "dispatched")

    def test_a_product_disabled_after_dispatch_refuses_the_write(self):
        key = self.dispatch_create()
        self.directory.set_product_state(TENANT, PRODUCT, "disabled")
        self.assert_refused_without_writing(key, "access_product_disabled")

    def test_an_organization_suspended_after_dispatch_refuses_the_write(self):
        """Refused by authentication before the handler; the guard would refuse it too."""
        key = self.dispatch_create()
        self.directory.set_organization_state(TENANT, "suspended")
        self.assertEqual(self.post_keyed(key).status_code, 403)
        self.assertEqual(self.keyed_count(), 0)
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

    def test_a_key_whose_workspace_was_revoked_fails_durably(self):
        key = self.dispatch_create()
        with db.get_connection() as connection:
            connection.execute("update record_grants set scope_ids = ? where user_id = ?",
                               ('["workspace-support"]', USER.user_id))
        self.assert_receipt(self.post_keyed(key), 409, "failed", "scope_unavailable")
        self.assertEqual(self.keyed_count(), 0)
        self.assertEqual(self.key_row(key)["result_code"], "scope_unavailable")


class KeylessWriteTest(ExecutionFixture):
    """Writes without a key are unchanged, and are not covered by the execution guarantee."""

    def test_a_write_without_a_key_still_works_and_records_nothing(self):
        response = self.post_form(FORM_ISSUE)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.issue_count("LIN-900"), 1)
        self.assertEqual(self.execution_rows(), [], "a keyless write must not invent an execution row")

    def test_a_keyless_write_is_still_scope_checked(self):
        with db.get_connection() as connection:
            connection.execute("update record_grants set scope_ids = ? where user_id = ?",
                               ('["workspace-support"]', USER.user_id))
        self.assertEqual(self.post_form(FORM_ISSUE).status_code, 403)
        self.assertEqual(self.issue_count("LIN-900"), 0)

    def test_a_keyless_form_body_is_still_validated(self):
        self.assertEqual(self.post_form({"title": ""}).status_code, 422)


class TransactionReviewTest(ExecutionFixture):
    """The two defects the second transaction review reproduced."""

    def test_one_approved_create_produces_exactly_one_new_record(self):
        before = self.table_counts()["demo_issues"]
        key = self.dispatch_create()
        self.assert_receipt(self.post_keyed(key), 200, "executed", "applied")
        self.assertEqual(self.table_counts()["demo_issues"], before + 1)
        self.assertEqual(self.key_row(key)["state"], "executed")

    def test_a_keyed_write_refuses_the_legacy_retry_header(self):
        """Two idempotency mechanisms could report an older receipt as this action's outcome."""
        key = self.dispatch_create()
        response = self.client.post("/api/demo-data/issues", json={"fields": create_fields()},
                                    headers={**self.headers(key), "Idempotency-Key": "legacy-1"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.keyed_count(), 0)
        self.assertEqual(self.key_row(key)["state"], "dispatched")
        update_key = self.dispatch_update("PIX-1", {"priority": "Low"}, turn_id=2)
        response = self.client.patch("/api/demo-data/issues/PIX-1", json={"changes": {"priority": "Low"}},
                                     headers={**self.headers(update_key), "Idempotency-Key": "legacy-2"})
        self.assertEqual(response.status_code, 400)

    def test_a_keyed_create_cannot_settle_on_an_older_receipt(self):
        """The reproduction: a manual create, then the same action keyed with the same header."""
        shared = "shared-receipt"
        self.assertEqual(self.post_form(FORM_ISSUE, **{"Idempotency-Key": shared}).status_code, 200)
        key = self.dispatch_create()
        keyed = self.client.post("/api/demo-data/issues", json={"fields": create_fields()},
                                 headers={**self.headers(key), "Idempotency-Key": shared})
        self.assertNotEqual(keyed.status_code, 200, "this must not report a create that did not happen")
        self.assertNotEqual(self.key_row(key)["state"], "executed")

    def test_a_keyed_write_without_the_legacy_header_is_unaffected(self):
        key = self.dispatch_create()
        self.assert_receipt(self.post_keyed(key), 200, "executed", "applied")
        self.assertEqual(self.keyed_count(), 1)

    def test_a_keyless_write_may_still_use_the_legacy_header(self):
        """The old mechanism is untouched where it is the only one."""
        first = self.post_form(FORM_ISSUE, **{"Idempotency-Key": "legacy-3"})
        second = self.post_form(FORM_ISSUE, **{"Idempotency-Key": "legacy-3"})
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.issue_count("LIN-900"), 1)

    def test_a_refused_request_leaves_every_record_table_untouched(self):
        """An unknown key, a foreign key, a missing session and a lost grant: none may mutate."""
        valid = self.dispatch_create()
        self.sessions.ensure_session("s-theirs", PRODUCT, OTHER_USER.user_id, TENANT, SCOPE, pin=self.pin)
        attempts = {
            "unknown key": lambda: self.post_keyed("not-a-key"),
            "another caller's key": lambda: self.post_keyed(valid, user=OTHER_USER, session="s-theirs"),
            "no session": lambda: self.post_keyed(valid, session=None),
            "legacy header as well": lambda: self.client.post(
                "/api/demo-data/issues", json={"fields": create_fields()},
                headers={**self.headers(valid), "Idempotency-Key": "legacy-4"}),
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
        self.assertEqual(self.post_keyed("not-a-key").status_code, 404)
        self.assertEqual(self.table_counts(), before)
        self.assertEqual(before, dict.fromkeys(RECORD_TABLES, 0))


if __name__ == "__main__":
    unittest.main()
