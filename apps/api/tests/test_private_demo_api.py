"""End-to-end contract for disposable, per-visitor public demo instances."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app import db
from app.definitions.sessions import DefinitionUnavailable
from app.main import agent, app, readiness
from app.services import env as env_module


TENANT = "pixel-dev"
PRODUCT = "linear-demo"
SCOPE = "workspace-product-eng"


class PrivateDemoApiTest(unittest.TestCase):
    def setUp(self):
        env_files = patch.object(env_module, "_env_files", lambda: ())
        env_files.start()
        self.addCleanup(env_files.stop)
        environment = patch.dict(os.environ, {
            "LLM_ENABLED": "false",
            "PIXEL_SYNTHETIC_DEMO": "true",
            "PIXEL_DEMO_SEEDS": "true",
            "PIXEL_PAID_PROVIDERS_ENABLED": "false",
        })
        environment.start()
        self.addCleanup(environment.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = patch.object(db, "DB_PATH", Path(temporary.name) / "private-demo.sqlite3")
        database.start()
        self.addCleanup(database.stop)
        db.migrate()
        readiness.invalidate()
        self.client = TestClient(app, raise_server_exceptions=False)

    def visitor(self) -> tuple[dict, dict[str, str]]:
        response = self.client.post(
            f"/api/organizations/{TENANT}/products/{PRODUCT}/visitor-sessions"
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        return body, {"Authorization": f"Bearer {body['token']}"}

    def data(self, headers: dict[str, str]) -> dict:
        response = self.client.get("/api/demo-data", headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def turn(self, headers: dict[str, str], message: str, *, session_id: str | None = None) -> dict:
        response = self.client.post("/api/turn", headers=headers, json={
            "session_id": session_id or f"private-{uuid4()}",
            "turn_id": 1,
            "product_id": PRODUCT,
            "message": message,
            "input_mode": "text",
            "current_page": "dashboard",
            "workspace_scope_id": SCOPE,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def issue(data: dict, issue_id: str = "LIN-142") -> dict:
        return next(issue for issue in data["issues"] if issue["id"] == issue_id)

    def test_each_visitor_gets_the_same_workflow_in_a_different_instance(self):
        first, first_headers = self.visitor()
        second, second_headers = self.visitor()

        self.assertNotEqual(first["visitor_id"], second["visitor_id"])
        self.assertNotEqual(first["instance_id"], second["instance_id"])
        self.assertEqual(first["generation"], second["generation"])
        self.assertEqual(self.data(first_headers), self.data(second_headers))

    def test_record_changes_and_chat_targeting_never_cross_visitors(self):
        _, first_headers = self.visitor()
        _, second_headers = self.visitor()
        first_issue = self.issue(self.data(first_headers))
        changed = {**first_issue, "assignee": "Noah Patel"}
        response = self.client.put(
            "/api/demo-data/issues/LIN-142", headers=first_headers, json=changed
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["revision"], first_issue["revision"] + 1)

        self.assertEqual(self.issue(self.data(first_headers))["assignee"], "Noah Patel")
        self.assertEqual(self.issue(self.data(second_headers))["assignee"], "Maya Chen")

        first_turn = self.turn(first_headers, "Open Maya's ticket")
        second_turn = self.turn(second_headers, "Open Maya's ticket")
        self.assertNotEqual(
            (first_turn.get("validated_action") or {}).get("type"), "OPEN_DEMO_ISSUE"
        )
        self.assertEqual(second_turn["validated_action"]["type"], "OPEN_DEMO_ISSUE")
        self.assertEqual(second_turn["validated_action"]["payload"]["issue_id"], "LIN-142")

    def test_new_records_exist_only_in_the_creating_visitor_instance(self):
        _, first_headers = self.visitor()
        _, second_headers = self.visitor()
        project = {
            "id": "",
            "name": "Visitor-only project",
            "description": "Synthetic private work",
            "progress": 0,
            "status": "Planned",
            "lead": "Maya Chen",
            "team": "Product Engineering",
            "targetDate": "2026-12-01",
        }
        response = self.client.post(
            f"/api/demo-data/projects?workspace_scope_id={SCOPE}",
            headers={**first_headers, "Idempotency-Key": "private-project"},
            json=project,
        )
        self.assertEqual(response.status_code, 200, response.text)
        created = response.json()
        self.assertIn(created["id"], {row["id"] for row in self.data(first_headers)["projects"]})
        self.assertNotIn(created["id"], {row["id"] for row in self.data(second_headers)["projects"]})

    def test_reset_restores_only_its_instance_and_rotates_the_generation(self):
        first, first_headers = self.visitor()
        _, second_headers = self.visitor()
        for headers in (first_headers, second_headers):
            issue = self.issue(self.data(headers))
            response = self.client.put(
                "/api/demo-data/issues/LIN-142",
                headers=headers,
                json={**issue, "assignee": "Noah Patel"},
            )
            self.assertEqual(response.status_code, 200, response.text)

        session_id = f"before-reset-{uuid4()}"
        self.turn(first_headers, "Show sprint planning", session_id=session_id)
        reset = self.client.post("/api/demo-data/reset-mine", headers=first_headers)
        self.assertEqual(reset.status_code, 200, reset.text)
        body = reset.json()
        self.assertEqual(body["instance_id"], first["instance_id"])
        self.assertEqual(body["generation"], first["generation"] + 1)
        self.assertEqual(self.issue(body["data"])["assignee"], "Maya Chen")

        # The previous token and generation are dead. The other visitor is unchanged.
        self.assertEqual(self.client.get("/api/demo-data", headers=first_headers).status_code, 401)
        self.assertEqual(self.issue(self.data(second_headers))["assignee"], "Noah Patel")
        new_headers = {"Authorization": f"Bearer {body['token']}"}
        self.assertEqual(self.issue(self.data(new_headers))["assignee"], "Maya Chen")
        cancelled = self.client.post(
            "/api/turn/1/cancel", headers=new_headers, json={"session_id": session_id}
        )
        self.assertEqual(cancelled.status_code, 404)

    def test_stale_revision_is_rejected_without_overwriting_the_newer_change(self):
        _, headers = self.visitor()
        issue = self.issue(self.data(headers))
        first = self.client.put(
            "/api/demo-data/issues/LIN-142",
            headers=headers,
            json={**issue, "priority": "Low"},
        )
        self.assertEqual(first.status_code, 200, first.text)
        stale = self.client.put(
            "/api/demo-data/issues/LIN-142",
            headers=headers,
            json={**issue, "priority": "Medium"},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.issue(self.data(headers))["priority"], "Low")

    def test_visitors_cannot_use_member_login_or_global_reset_privileges(self):
        _, headers = self.visitor()
        self.assertEqual(self.client.post("/api/demo-data/reset", headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/api/usage/summary", headers=headers).status_code, 403)

    def test_visitors_cannot_continue_or_cancel_another_private_conversation(self):
        _, first_headers = self.visitor()
        _, second_headers = self.visitor()
        session_id = f"owned-{uuid4()}"
        self.turn(first_headers, "Show sprint planning", session_id=session_id)

        crossed = self.turn(second_headers, "Open Maya's ticket", session_id=session_id)
        self.assertEqual(crossed["status"], "denied")
        self.assertIsNone(crossed["validated_action"])
        self.assertIn("owned by another", crossed["intent_trace"]["reason"])
        cancelled = self.client.post(
            "/api/turn/1/cancel", headers=second_headers, json={"session_id": session_id}
        )
        self.assertEqual(cancelled.status_code, 404)

    def test_invalid_definition_does_not_allocate_an_orphan_instance(self):
        with patch("app.auth.pin_new_session", side_effect=DefinitionUnavailable("invalid")):
            response = self.client.post(
                f"/api/organizations/{TENANT}/products/{PRODUCT}/visitor-sessions"
            )
        self.assertEqual(response.status_code, 404)
        with db.get_connection() as connection:
            self.assertEqual(
                connection.execute("select count(*) from demo_instances").fetchone()[0], 0
            )


if __name__ == "__main__":
    unittest.main()
