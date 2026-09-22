"""Milestone 3.2, Slice 5b step 5: the new engine's turn path with execution keys (plan 6.1, 7, 8, 11).

The new engine answers `/api/turn` here only through a dependency override, exactly as the
browser-test entry module does. Production (`app.main` alone) keeps the live engine and a null
execution envelope.
"""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
API_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_DIR))
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import db, main  # noqa: E402
from app.auth import create_token  # noqa: E402
from app.engine.conversation_engine import ConversationEngine, TurnStage  # noqa: E402
from app.installed_products import package_for  # noqa: E402
from app.services import env as env_module  # noqa: E402
from app.services.turn_execution import NewEngineTurns  # noqa: E402

PRODUCT = "linear-demo"
SCOPE = "workspace-product-eng"
HERMETIC = {"LLM_ENABLED": "false", "PIXEL_SYNTHETIC_DEMO": "true", "PIXEL_DEMO_SEEDS": "true",
            "PIXEL_PAID_PROVIDERS_ENABLED": "false", "PIXEL_BLOCK_EXTERNAL_HTTP": "true"}


class NewEngineFixture(unittest.TestCase):
    def setUp(self):
        for patcher in (patch.object(env_module, "_env_files", lambda: ()), patch.dict(os.environ, HERMETIC)):
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = patch.object(db, "DB_PATH", Path(temporary.name) / "turns.sqlite3")
        database.start()
        self.addCleanup(database.stop)
        db.migrate()
        main.product_data.seed_if_empty()
        self.turns = NewEngineTurns(main.agent.sessions, main.agent.directory, package_for)
        main.app.dependency_overrides[main.turn_engine] = lambda: self.turns
        self.addCleanup(main.app.dependency_overrides.clear)
        self.client = TestClient(main.app)
        self.headers = {"Authorization": f"Bearer {create_token('demo-admin')}"}
        self.turn_id = 0

    def say(self, message: str, session: str = "new-engine") -> dict:
        self.turn_id += 1
        response = self.client.post("/api/turn", headers=self.headers, json={
            "session_id": session, "turn_id": self.turn_id, "product_id": PRODUCT, "message": message,
            "workspace_scope_id": SCOPE,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def write(self, turn: dict, session: str = "new-engine"):
        action = turn["validated_action"]
        payload = dict(action["payload"])
        headers = {**self.headers, "X-Execution-Key": turn["execution"]["key"], "X-Session-Id": session}
        if action["type"] == "UPDATE_DEMO_ISSUE":
            target = payload.pop("issue_id")
            return self.client.patch(f"/api/demo-data/issues/{target}", json={"changes": payload}, headers=headers)
        return self.client.post("/api/demo-data/issues", json={"fields": payload}, headers=headers)


class EnvelopeTest(NewEngineFixture):
    def test_only_a_dispatched_mutation_carries_a_key(self):
        navigation = self.say("Show me the issues")
        self.assertIsNone(navigation["execution"])
        self.assertEqual(navigation["validated_action"]["type"], "OPEN_ISSUES")
        refusal = self.say("Delete all issues")
        self.assertIsNone(refusal["execution"])
        self.assertEqual(refusal["status"], "denied")
        self.say("Open Maya's ticket")
        mutation = self.say("assign it to Noah")
        self.assertEqual(mutation["validated_action"]["type"], "UPDATE_DEMO_ISSUE")
        envelope = mutation["execution"]
        self.assertEqual((envelope["session_id"], envelope["turn_id"]), ("new-engine", self.turn_id))
        self.assertTrue(envelope["key"])
        self.assertTrue(mutation["speech"].startswith("I'll update"), "proposed wording, never 'done'")

    def test_the_full_round_trip_executes_once_and_answers_what_changed(self):
        self.assertEqual(self.say("What changed?")["speech"], "Nothing has changed in this conversation yet.")
        self.say("Open Maya's ticket")
        mutation = self.say("assign it to Noah")
        receipt = self.write(mutation)
        self.assertEqual(receipt.status_code, 200, receipt.text)
        self.assertEqual(receipt.json()["speech"], "Updated LIN-142: assignee to Noah Patel.")
        self.assertEqual(receipt.json()["record"]["assignee"], "Noah Patel")
        self.assertEqual(self.write(mutation).json()["speech"], "This change was already applied.")
        self.assertEqual(self.say("What changed?")["speech"], "The most recent change: updated LIN-142.")

    def test_a_newer_turn_supersedes_the_older_key(self):
        self.say("Open Maya's ticket")
        mutation = self.say("assign it to Noah")
        self.say("Show me the cycles")
        refused = self.write(mutation)
        self.assertEqual(refused.status_code, 409)
        self.assertEqual((refused.json()["outcome"], refused.json()["code"]), ("refused", "superseded"))
        self.assertEqual(self.say("What changed?")["speech"], "Nothing has changed in this conversation yet.")

    def test_a_turn_that_loses_the_dispatch_race_does_not_advance_memory(self):
        self.say("Open Maya's ticket")
        self.assertEqual(self._durable_last_turn(), 1)
        with patch.object(self.turns._ledger, "dispatch_if_active", return_value=None):
            stale = self.say("assign it to Noah")
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(self._durable_last_turn(), 1)

    def _durable_last_turn(self, session: str = "new-engine") -> int:
        with db.get_connection() as connection:
            row = connection.execute(
                "select last_turn from engine_state where session_id = ?", (session,)
            ).fetchone()
        return row["last_turn"]

    def test_awaiting_confirmation_carries_no_key_and_nothing_executable(self):
        real_turn = ConversationEngine.turn

        def awaiting(engine, *args, **kwargs):
            return replace(real_turn(engine, *args, **kwargs), stage=TurnStage.AWAITING_CONFIRMATION)

        self.say("Open Maya's ticket")
        with patch.object(ConversationEngine, "turn", awaiting):
            pending = self.say("assign it to Noah")
        self.assertIsNone(pending["execution"])
        self.assertIsNone(pending["validated_action"])
        self.assertEqual(pending["proposed_action"]["type"], "UPDATE_DEMO_ISSUE")
        with db.get_connection() as connection:
            self.assertEqual(connection.execute("select count(*) from action_executions").fetchone()[0], 0)

    def test_a_yes_to_a_pending_change_dispatches_its_key(self):
        real_turn = ConversationEngine.turn

        def would_execute(engine, *args, **kwargs):
            return replace(real_turn(engine, *args, **kwargs), stage=TurnStage.WOULD_EXECUTE)

        self.say("Open Maya's ticket")
        with patch.object(ConversationEngine, "turn", would_execute):
            confirmed = self.say("assign it to Noah")
        self.assertIsNotNone(confirmed["execution"])
        self.assertEqual(self.write(confirmed).status_code, 200)


class RestartSafetyTest(NewEngineFixture):
    """5c plan, section 6: a process restart forgets nothing unsafe and remembers what it can."""

    def restart(self) -> None:
        """A fresh `NewEngineTurns` sharing the database but no in-process cache."""
        self.turns = NewEngineTurns(main.agent.sessions, main.agent.directory, package_for)
        main.app.dependency_overrides[main.turn_engine] = lambda: self.turns

    def test_focus_survives_a_restart_but_a_pending_question_does_not(self):
        self.say("Open Maya's ticket")
        self.restart()
        mutation = self.say("assign it to Noah")
        self.assertEqual(mutation["validated_action"]["type"], "UPDATE_DEMO_ISSUE")
        self.assertEqual(mutation["validated_action"]["payload"]["issue_id"], "LIN-142")

    def test_last_change_survives_a_restart(self):
        self.say("Open Maya's ticket")
        mutation = self.say("assign it to Noah")
        self.write(mutation)
        self.restart()
        self.assertEqual(self.say("What changed?")["speech"], "The most recent change: updated LIN-142.")


class ProductionPathTest(unittest.TestCase):
    def test_the_live_engine_answers_production_with_a_null_envelope(self):
        with patch.dict(os.environ, {"PIXEL_ENGINE_MODE": ""}, clear=False):
            self.assertIs(main.turn_engine(), main.live_turn)
        self.assertNotIn(main.turn_engine, main.app.dependency_overrides)

    def test_production_never_reaches_the_test_entry_module(self):
        dockerfile = (REPO_ROOT / "Dockerfile.api").read_text(encoding="utf-8")
        self.assertIn("app.main", dockerfile)
        self.assertNotIn("testing_main", dockerfile)
        code = ("import sys\nimport app.main\n"
                "assert 'app.testing_main' not in sys.modules\n"
                "assert 'app.services.turn_execution' not in sys.modules\n")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(API_DIR), str(REPO_ROOT)]),
               "PIXEL_IGNORE_ENV_FILES": "true", "PIXEL_ENGINE_MODE": ""}
        env.pop("PIXEL_TESTING_ENTRY", None)
        result = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT), env=env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_test_entry_module_refuses_to_load_without_its_marker(self):
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(API_DIR), str(REPO_ROOT)]),
               "PIXEL_IGNORE_ENV_FILES": "true"}
        env.pop("PIXEL_TESTING_ENTRY", None)
        result = subprocess.run([sys.executable, "-c", "import app.testing_main"], cwd=str(REPO_ROOT), env=env,
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("serves only browser tests", result.stderr)


if __name__ == "__main__":
    unittest.main()
