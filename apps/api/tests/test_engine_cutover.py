"""Milestone 3.2, Slice 5c: authoritative engine selection and rollback switch."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from fastapi.testclient import TestClient

from app import db, main
from app.auth import create_token
from app.services import env as env_module

PRODUCT = "linear-demo"
SCOPE = "workspace-product-eng"
HERMETIC = {
    "LLM_ENABLED": "false",
    "PIXEL_SYNTHETIC_DEMO": "true",
    "PIXEL_DEMO_SEEDS": "true",
    "PIXEL_PAID_PROVIDERS_ENABLED": "false",
    "PIXEL_BLOCK_EXTERNAL_HTTP": "true",
}


class EngineCutoverFixture(unittest.TestCase):
    def setUp(self):
        files_patch = patch.object(env_module, "_env_files", lambda: ())
        files_patch.start()
        self.addCleanup(files_patch.stop)
        env_patch = patch.dict(os.environ, {**HERMETIC, "PIXEL_ENGINE_MODE": ""}, clear=False)
        env_patch.start()
        self.addCleanup(env_patch.stop)

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = patch.object(db, "DB_PATH", Path(temporary.name) / "cutover.sqlite3")
        database.start()
        self.addCleanup(database.stop)
        db.migrate()
        main.product_data.seed_if_empty()
        main.readiness.invalidate()
        main.reset_definition_turns_for_tests()
        self.addCleanup(main.readiness.invalidate)
        self.addCleanup(main.reset_definition_turns_for_tests)
        main.app.dependency_overrides.clear()
        self.addCleanup(main.app.dependency_overrides.clear)
        self.client = TestClient(main.app)
        self.headers = {"Authorization": f"Bearer {create_token('demo-admin')}"}

    def turn(self, message: str, *, session: str = "cutover", turn_id: int = 1):
        return self.client.post("/api/turn", headers=self.headers, json={
            "session_id": session,
            "turn_id": turn_id,
            "product_id": PRODUCT,
            "message": message,
            "input_mode": "text",
            "current_page": "dashboard",
            "workspace_scope_id": SCOPE,
        })


class EngineCutoverTest(EngineCutoverFixture):
    def test_legacy_is_the_default_authority_path(self):
        env = {**HERMETIC, "PIXEL_ENGINE_MODE": ""}
        with patch.dict(os.environ, env, clear=False):
            self.assertIs(main.turn_engine(), main.live_turn)
            response = self.turn("Show sprint planning")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertIsNone(body["execution"])

    def test_definition_mode_is_production_cutover_without_the_testing_entry_module(self):
        env = {**HERMETIC, "PIXEL_ENGINE_MODE": "definition"}
        with patch.dict(os.environ, env, clear=False):
            engine = main.turn_engine()
            self.assertIsNot(engine, main.live_turn)
            self.assertEqual(engine.__class__.__name__, "NewEngineTurns")
            response = self.turn("Show me the issues")

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "OPEN_ISSUES")
        self.assertIsNone(body["execution"])
        self.assertIn("New engine", body["intent_trace"]["reason"])
        self.assertNotIn("app.testing_main", sys.modules)

    def test_definition_mode_ignores_the_shadow_switch(self):
        env = {**HERMETIC, "PIXEL_ENGINE_MODE": "definition", "PIXEL_SHADOW_ENGINE": "on"}
        with patch.dict(os.environ, env, clear=False), patch.object(main, "shadow_controller") as shadow:
            response = self.turn("Show me the issues")

        self.assertEqual(response.status_code, 200, response.text)
        shadow.assert_not_called()

    def test_invalid_engine_mode_makes_health_unhealthy(self):
        env = {**HERMETIC, "PIXEL_ENGINE_MODE": "definiton"}
        with patch.dict(os.environ, env, clear=False):
            response = self.client.get("/health")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "unhealthy", "reason": "invalid_engine_mode"})

    def test_invalid_engine_mode_blocks_turn_traffic(self):
        env = {**HERMETIC, "PIXEL_ENGINE_MODE": "definiton"}
        with patch.dict(os.environ, env, clear=False):
            response = self.turn("Show sprint planning")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Invalid engine authority mode.")


class LegacyMutationTest(EngineCutoverFixture):
    """5c plan, section 3.3 and 9: rollback (legacy authority) never re-enables a keyless
    assistant write, and never claims a change applied before it actually did."""

    def test_a_legacy_update_dispatches_a_key_instead_of_claiming_it_already_happened(self):
        mutation = self.turn("assign LIN-142 to Noah", turn_id=1)
        self.assertEqual(mutation.status_code, 200, mutation.text)
        body = mutation.json()
        self.assertEqual(body["validated_action"]["type"], "UPDATE_DEMO_ISSUE")
        self.assertIsNotNone(body["execution"])
        self.assertTrue(body["speech"].startswith("I'll update"), body["speech"])
        self.assertNotIn("Done", body["speech"])

        headers = {**self.headers, "X-Execution-Key": body["execution"]["key"], "X-Session-Id": "cutover"}
        receipt = self.client.patch(
            "/api/demo-data/issues/LIN-142", json={"changes": {"assignee": "Noah Patel"}}, headers=headers,
        )
        self.assertEqual(receipt.status_code, 200, receipt.text)
        self.assertEqual(receipt.json()["outcome"], "executed")
        self.assertEqual(receipt.json()["record"]["assignee"], "Noah Patel")

        # Without the key, the same change is refused rather than written.
        form = self.client.put(
            "/api/demo-data/issues/LIN-142",
            json={"id": "LIN-142", "title": "x", "priority": "Medium", "assignee": "Someone Else",
                  "project": "Atlas Platform", "status": "Todo"},
            headers={**self.headers, "X-Execution-Key": body["execution"]["key"]},
        )
        self.assertEqual(form.status_code, 400)

    def test_a_legacy_create_hands_back_exactly_the_bound_change_and_commits(self):
        mutation = self.turn("open a fresh ticket for Maya", turn_id=1).json()
        self.assertEqual(mutation["validated_action"]["type"], "CREATE_DEMO_ISSUE")
        payload = mutation["validated_action"]["payload"]
        self.assertNotIn("id", payload, "the server assigns the ID; the client never proposes one")
        headers = {**self.headers, "X-Execution-Key": mutation["execution"]["key"], "X-Session-Id": "cutover"}
        receipt = self.client.post("/api/demo-data/issues", json={"fields": payload}, headers=headers)
        self.assertEqual(receipt.status_code, 200, receipt.text)
        body = receipt.json()
        self.assertEqual(body["outcome"], "executed")
        self.assertEqual(body["record"]["assignee"], "Maya Chen")
        self.assertTrue(body["speech"].startswith(f"Created {body['record']['id']}:"), body["speech"])

    def test_a_key_superseded_by_a_later_turn_is_refused_not_written(self):
        """The key dispatched for turn 1 is still valid until a later turn is activated; once
        turn 2 activates, `activate_turn`'s own supersession cancels it (5b plan, section 6.1)."""
        mutation = self.turn("assign LIN-142 to Noah", turn_id=1)
        self.assertIsNotNone(mutation.json()["execution"])
        self.turn("Show the cycles", turn_id=2)

        headers = {**self.headers, "X-Execution-Key": mutation.json()["execution"]["key"],
                  "X-Session-Id": "cutover"}
        receipt = self.client.patch(
            "/api/demo-data/issues/LIN-142", json={"changes": {"assignee": "Noah Patel"}}, headers=headers,
        )
        self.assertEqual(receipt.status_code, 409, receipt.text)
        self.assertEqual(receipt.json()["code"], "superseded")

class ChangeSetTest(unittest.TestCase):
    """Unit coverage for the adapter's dict shape, independent of the turn service."""

    def test_a_non_mutation_type_is_not_a_keyed_change(self):
        from app.services.legacy_adapter import change_set_for

        self.assertIsNone(change_set_for("OPEN_DEMO_ISSUE", {"issue_id": "LIN-142"}))

    def test_an_update_with_no_changed_fields_is_not_a_keyed_change(self):
        from app.services.legacy_adapter import change_set_for

        self.assertIsNone(change_set_for("UPDATE_DEMO_ISSUE", {"issue_id": "LIN-142"}))

    def test_an_update_names_its_target_and_only_the_changed_fields(self):
        from app.services.legacy_adapter import change_set_for

        result = change_set_for("UPDATE_DEMO_ISSUE", {"issue_id": "LIN-142", "assignee": "Noah Patel"})
        self.assertEqual(result, {"action": "update_issue", "target": "LIN-142",
                                   "changes": {"assignee": "Noah Patel"}})

    def test_a_create_drops_the_legacy_assigned_id(self):
        from app.services.legacy_adapter import change_set_for

        result = change_set_for("CREATE_DEMO_ISSUE", {
            "id": "PIX-200", "title": "New work", "priority": "Medium", "assignee": "Noah Patel",
            "project": "Atlas Platform", "status": "Todo",
        })
        self.assertNotIn("id", result["fields"])
        self.assertEqual(result["action"], "create_issue")
        self.assertEqual(result["fields"]["title"], "New work")


if __name__ == "__main__":
    unittest.main()
