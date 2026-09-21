"""GAP-01 authentication tests. These were expectedFailure during the audit;
they now pass because every data endpoint requires a bearer token."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app import db
from app.auth import create_token
from app.main import app
from app.services import env as env_module
from app.services.agent_reasoner import AgentReasoner
from app.services.demo_data import issue_in_scope, load_demo_issues
from app.services.product_data_store import ProductDataStore
from app.services.session_manager import SessionManager
from app.workspace_config import get_workspace_scope


class PhaseOneGapTest(unittest.TestCase):
    def setUp(self):
        env_patch = patch.dict(os.environ, {"LLM_ENABLED": "false", "PIXEL_DEMO_SEEDS": "true"})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.temp_dir = temporary.name
        db_patch = patch.object(db, "DB_PATH", Path(temporary.name) / "audit.sqlite3")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        db.migrate()
        self.client = TestClient(app, raise_server_exceptions=False)

    def _auth_header(self, user_id: str = "demo-product-eng") -> dict[str, str]:
        token = create_token(user_id)
        return {"Authorization": f"Bearer {token}"}

    # --- GAP-01: anonymous access must be rejected ---

    def test_gap_01_anonymous_client_cannot_read_customer_data(self):
        self.assertIn(self.client.get("/api/demo-data").status_code, (401, 403))

    def test_gap_01_anonymous_client_cannot_reset_customer_data(self):
        self.assertIn(self.client.post("/api/demo-data/reset").status_code, (401, 403))

    def test_gap_01_anonymous_client_cannot_create_issue(self):
        self.assertIn(
            self.client.post("/api/demo-data/issues", json={"title": "x"}).status_code,
            (401, 403, 422),
        )

    def test_gap_01_anonymous_client_cannot_send_turn(self):
        self.assertIn(
            self.client.post("/api/turn", json={"message": "hello"}).status_code,
            (401, 403, 422),
        )

    # --- GAP-01: authenticated access works ---

    def test_gap_01_authenticated_client_can_read_own_data(self):
        response = self.client.get("/api/demo-data", headers=self._auth_header())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("issues", data)
        self.assertIn("projects", data)

    def test_gap_01_non_admin_cannot_reset_data(self):
        response = self.client.post(
            "/api/demo-data/reset", headers=self._auth_header("demo-product-eng")
        )
        self.assertEqual(response.status_code, 403)

    def test_gap_01_admin_can_reset_data(self):
        response = self.client.post(
            "/api/demo-data/reset", headers=self._auth_header("demo-admin")
        )
        self.assertEqual(response.status_code, 200)

    def test_gap_01_scope_enforcement_blocks_wrong_workspace(self):
        """A product-eng user cannot create a project in the platform workspace."""
        project = {
            "name": "Blocked Project",
            "description": "Should not be created",
            "progress": 0,
            "status": "Planned",
            "lead": "Maya Chen",
            "team": "Product Engineering",
            "targetDate": "2026-12-01",
        }
        response = self.client.post(
            "/api/demo-data/projects?workspace_scope_id=workspace-platform",
            json=project,
            headers=self._auth_header("demo-product-eng"),
        )
        self.assertEqual(response.status_code, 403)

    # --- Milestone 1: defects reproduced after Phase 1 ---

    def test_m1_demo_login_requires_explicit_synthetic_demo_flag(self):
        # A non-administrator: whether an administrator may use the demo is a separate rule.
        body = {"user_id": "demo-product-eng"}
        with patch.object(env_module, "_env_files", lambda: ()), patch.dict(os.environ):
            os.environ.pop("PIXEL_SYNTHETIC_DEMO", None)
            self.assertEqual(self.client.post("/api/auth/demo-login", json=body).status_code, 403)
            os.environ["PIXEL_SYNTHETIC_DEMO"] = "false"
            self.assertEqual(self.client.post("/api/auth/demo-login", json=body).status_code, 403)
            os.environ["PIXEL_SYNTHETIC_DEMO"] = "true"
            self.assertEqual(self.client.post("/api/auth/demo-login", json=body).status_code, 200)

    def test_m1_same_named_project_does_not_expand_agent_context(self):
        # Platform owns the "Planning" issues; a Product Engineering project with the
        # same name must not make them visible to Product Engineering.
        ProductDataStore().save_project(_project(name="Planning"), "workspace-product-eng")
        # Lookups read the caller's own records, passed explicitly; here, the shared member demo.
        data = ProductDataStore().load()
        scope = get_workspace_scope("workspace-product-eng", data)
        platform_issues = [issue for issue in load_demo_issues(data) if issue.projectId in {"PRJ-103", "PRJ-104"}]
        self.assertTrue(platform_issues)

        leaked = [
            issue.id for issue in platform_issues
            if issue_in_scope(issue, set(scope.allowed_project_ids), set(scope.allowed_issue_projects))
        ]
        self.assertEqual(leaked, [])

        model_context = AgentReasoner()._visible_workspace_data(scope, data)["issues"]
        platform_ids = {issue.id for issue in platform_issues}
        self.assertEqual([line for line in model_context if line.split()[0] in platform_ids], [])

    def test_m1_member_write_rejects_project_outside_workspace(self):
        member = {
            "name": "Scope Breaker", "initials": "SB", "role": "Engineer", "load": 10,
            "projectIds": ["PRJ-101", "PRJ-103"],
        }
        for user_id in ("demo-product-eng", "demo-admin"):
            response = self.client.post(
                "/api/demo-data/team-members?workspace_scope_id=workspace-product-eng",
                json=member,
                headers=self._auth_header(user_id),
            )
            self.assertEqual(response.status_code, 403, user_id)
        self.assertNotIn("Scope Breaker", {m["name"] for m in ProductDataStore().load()["team"]})

    def test_m1_issue_writes_reject_missing_or_out_of_scope_assignee(self):
        admin = self._auth_header("demo-admin")
        issue = next(issue for issue in ProductDataStore().load()["issues"] if issue["projectId"] == "PRJ-101")
        for assignee in ("Nobody Here", "Avery Brooks"):
            updated = self.client.put(
                f"/api/demo-data/issues/{issue['id']}", json={**issue, "assignee": assignee}, headers=admin
            )
            self.assertEqual(updated.status_code, 422, assignee)
            created = self.client.post("/api/demo-data/issues", json=_issue(assignee=assignee), headers=admin)
            self.assertEqual(created.status_code, 422, assignee)
        self.assertEqual(ProductDataStore().get_issue(issue["id"])["assignee"], issue["assignee"])

    def test_m1_unchanged_historical_assignee_can_still_be_edited(self):
        issue = next(issue for issue in ProductDataStore().load()["issues"] if issue["assignee"] == "Sam Rivera")
        response = self.client.put(
            f"/api/demo-data/issues/{issue['id']}",
            json={**issue, "title": "Edited historical ticket"},
            headers=self._auth_header("demo-admin"),
        )
        self.assertEqual(response.status_code, 200)

    def test_m1_workspace_move_revalidates_unchanged_assignee(self):
        store = ProductDataStore()
        for assignee in ("Maya Chen", "Sam Rivera"):
            with self.subTest(assignee=assignee):
                issue = next(i for i in store.load()["issues"] if i["assignee"] == assignee)
                response = self.client.put(
                    f"/api/demo-data/issues/{issue['id']}",
                    json={**issue, "projectId": "PRJ-103", "project": "Planning"},
                    headers=self._auth_header("demo-admin"),
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(store.get_issue(issue["id"]), issue)

    def test_m1_workspace_move_accepts_destination_assignee(self):
        store = ProductDataStore()
        issue = next(i for i in store.load()["issues"] if i["assignee"] == "Maya Chen")
        response = self.client.put(
            f"/api/demo-data/issues/{issue['id']}",
            json={**issue, "projectId": "PRJ-103", "project": "Planning", "assignee": "Avery Brooks"},
            headers=self._auth_header("demo-admin"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(store.get_issue(issue["id"])["assignee"], "Avery Brooks")

    def test_m1_historical_assignee_can_move_within_same_workspace(self):
        issue = next(i for i in ProductDataStore().load()["issues"] if i["assignee"] == "Sam Rivera")
        self.assertEqual(issue["projectId"], "PRJ-102")
        response = self.client.put(
            f"/api/demo-data/issues/{issue['id']}",
            json={**issue, "projectId": "PRJ-101", "project": "Integrations"},
            headers=self._auth_header("demo-admin"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(ProductDataStore().get_issue(issue["id"])["projectId"], "PRJ-101")

    def test_gap_01_scoped_user_only_reads_own_workspace(self):
        data = self.client.get("/api/demo-data", headers=self._auth_header()).json()
        self.assertEqual([scope["id"] for scope in data["workspaceScopes"]], ["workspace-product-eng"])
        self.assertEqual({project["id"] for project in data["projects"]}, {"PRJ-101", "PRJ-102"})
        self.assertTrue(data["issues"])
        self.assertTrue(all(issue["projectId"] in {"PRJ-101", "PRJ-102"} for issue in data["issues"]))
        self.assertTrue(all(cycle["projectId"] in {"PRJ-101", "PRJ-102"} for cycle in data["cycles"]))
        self.assertNotIn("Avery Brooks", {member["name"] for member in data["team"]})

    def test_gap_01_scoped_user_cannot_create_issue_in_other_workspace(self):
        response = self.client.post(
            "/api/demo-data/issues",
            json=_issue(projectId="PRJ-103", project="Planning", assignee="Avery Brooks"),
            headers=self._auth_header(),
        )
        self.assertEqual(response.status_code, 403)

    def test_gap_01_scoped_user_can_create_issue_in_own_workspace(self):
        response = self.client.post("/api/demo-data/issues", json=_issue(), headers=self._auth_header())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["id"].startswith("PIX-"))

    def test_gap_01_scoped_user_cannot_update_other_workspace_issue(self):
        issue = next(issue for issue in ProductDataStore().load()["issues"] if issue["projectId"] == "PRJ-103")
        payload = {**issue, "title": "Changed from the wrong workspace"}
        response = self.client.put(f"/api/demo-data/issues/{issue['id']}", json=payload, headers=self._auth_header())
        self.assertEqual(response.status_code, 403)

    def test_gap_01_scoped_user_cannot_move_issue_into_other_workspace(self):
        issue = next(issue for issue in ProductDataStore().load()["issues"] if issue["projectId"] == "PRJ-101")
        payload = {**issue, "projectId": "PRJ-103", "project": "Planning"}
        response = self.client.put(f"/api/demo-data/issues/{issue['id']}", json=payload, headers=self._auth_header())
        self.assertEqual(response.status_code, 403)

    def test_gap_01_scoped_user_cannot_create_cycle_in_other_workspace(self):
        for project_id in ("PRJ-103", None):
            response = self.client.post(
                "/api/demo-data/cycles", json=_cycle(projectId=project_id), headers=self._auth_header()
            )
            self.assertEqual(response.status_code, 403, project_id)

    def test_gap_01_scoped_user_cannot_chat_in_other_workspace(self):
        response = self.client.post(
            "/api/turn",
            json=_turn("session-scope", workspace_scope_id="workspace-platform"),
            headers=self._auth_header(),
        )
        self.assertEqual(response.status_code, 403)

    def test_gap_01_speech_requires_authentication(self):
        self.assertEqual(self.client.post("/api/speech", json={"text": "Hello"}).status_code, 401)

    # --- GAP-05: session ownership and cancellation ---

    def test_gap_05_other_user_cannot_continue_or_cancel_session(self):
        owner_turn = self.client.post(
            "/api/turn", json=_turn("session-owned"), headers=self._auth_header()
        )
        self.assertEqual(owner_turn.status_code, 200)

        hijack = self.client.post(
            "/api/turn",
            json=_turn("session-owned", turn_id=2, workspace_scope_id="workspace-platform"),
            headers=self._auth_header("demo-platform"),
        )
        self.assertEqual(hijack.json()["status"], "denied")

        cancel = self.client.post(
            "/api/turn/1/cancel",
            json={"session_id": "session-owned"},
            headers=self._auth_header("demo-platform"),
        )
        self.assertEqual(cancel.status_code, 404)

    def test_gap_05_cancelling_old_turn_keeps_newer_turn(self):
        sessions = SessionManager()
        sessions.ensure_session("session-race", "linear-demo")
        self.assertTrue(sessions.activate_turn("session-race", 1))
        self.assertTrue(sessions.activate_turn("session-race", 2))
        self.assertFalse(sessions.cancel_turn("session-race", 1))
        self.assertTrue(sessions.is_active_turn("session-race", 2))

    # --- GAP-03: record integrity ---

    def test_gap_03_issue_must_reference_existing_project_and_member(self):
        admin = self._auth_header("demo-admin")
        missing_project = self.client.post(
            "/api/demo-data/issues", json=_issue(projectId="PRJ-999"), headers=admin
        )
        self.assertEqual(missing_project.status_code, 422)
        missing_member = self.client.post(
            "/api/demo-data/issues", json=_issue(assignee="Nobody Here"), headers=admin
        )
        self.assertEqual(missing_member.status_code, 422)

    def test_gap_03_project_links_are_enforced_by_the_database(self):
        with db.get_connection() as connection:
            self.assertTrue(connection.execute("pragma foreign_key_list(demo_issues)").fetchall())
            self.assertTrue(connection.execute("pragma foreign_key_list(demo_cycles)").fetchall())
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "insert into demo_issues(id, title, priority, assignee, project, project_id, status) "
                    "values ('PIX-orphan', 'Orphan', 'Medium', 'Maya Chen', 'Integrations', 'PRJ-999', 'Todo')"
                )

    def test_gap_03_migration_adds_links_and_keeps_existing_rows(self):
        """A database created before project links is rebuilt without losing rows."""
        legacy_path = Path(self.temp_dir) / "legacy.sqlite3"
        legacy = sqlite3.connect(legacy_path)
        legacy.executescript(
            """
            create table demo_projects(
              id text primary key, name text not null, description text not null,
              progress integer not null, status text not null, lead text not null,
              team text not null, target_date text not null
            );
            create table demo_issues(
              id text primary key, title text not null, priority text not null,
              assignee text not null, project text not null, project_id text,
              status text not null, cycle text, estimate text, label text, description text
            );
            insert into demo_projects values ('PRJ-101', 'Kept', '', 10, 'Active', 'Maya Chen', 'PE', '2026-10-01');
            insert into demo_issues values ('PIX-1', 'Linked', 'Medium', 'Maya Chen', 'Integrations', 'PRJ-101', 'Todo', null, null, null, null);
            insert into demo_issues values ('PIX-2', 'Dangling', 'Medium', 'Maya Chen', 'Integrations', 'PRJ-gone', 'Todo', null, null, null, null);
            """
        )
        legacy.commit()
        legacy.close()

        with patch.object(db, "DB_PATH", legacy_path):
            db.migrate()
            with db.get_connection() as connection:
                self.assertTrue(connection.execute("pragma foreign_key_list(demo_issues)").fetchall())
                issues = {row["id"]: row["project_id"] for row in
                          connection.execute("select id, project_id from demo_issues").fetchall()}
                self.assertEqual(connection.execute("pragma foreign_key_check").fetchall(), [])
            backups = list((legacy_path.parent / "backups").glob("legacy-*.sqlite3"))

        # Both rows survive; only the link to the missing project is cleared.
        self.assertEqual(issues, {"PIX-1": "PRJ-101", "PIX-2": None})
        self.assertTrue(backups, "migration must leave a pre-migration backup")

    def test_gap_03_concurrent_creation_preserves_every_record(self):
        store = ProductDataStore()
        before = len(store.load()["projects"])

        def create(index: int) -> str:
            project = {
                "id": "", "name": f"Parallel {index}", "description": "", "progress": 0,
                "status": "Planned", "lead": "Maya Chen", "team": "Product Engineering",
                "targetDate": "2026-12-01",
            }
            return store.save_project(project, "workspace-product-eng")["id"]

        with ThreadPoolExecutor(max_workers=5) as pool:
            ids = list(pool.map(create, range(5)))
        self.assertEqual(len(set(ids)), 5)
        self.assertEqual(len(store.load()["projects"]), before + 5)

    def test_gap_03_invalid_issue_is_rejected_without_server_error(self):
        self.assertIn(self.client.post(
            "/api/demo-data/issues", json={}, headers=self._auth_header()
        ).status_code, (401, 403, 422))

    def test_gap_03_update_cannot_create_missing_record(self):
        issue = ProductDataStore().load()["issues"][0]
        self.assertIn(self.client.put(
            "/api/demo-data/issues/does-not-exist", json=issue,
            headers=self._auth_header()
        ).status_code, (401, 403, 404))


def _issue(**overrides) -> dict:
    return {
        "title": "Scoped ticket", "priority": "Medium", "assignee": "Maya Chen",
        "project": "Integrations", "projectId": "PRJ-101", "status": "Todo", **overrides,
    }


def _project(**overrides) -> dict:
    return {
        "name": "Scoped project", "description": "", "progress": 0, "status": "Planned",
        "lead": "Maya Chen", "team": "Product Engineering", "targetDate": "2026-12-01", **overrides,
    }


def _cycle(**overrides) -> dict:
    return {
        "name": "Scoped cycle", "projectId": "PRJ-101", "daysLeft": 10, "progress": 0,
        "completed": 0, "inProgress": 0, "remaining": 0, "focus": [], "status": "Planned",
        "team": "Product Engineering", "startDate": "2026-10-01", "endDate": "2026-10-14",
        **overrides,
    }


def _turn(session_id: str, turn_id: int = 1, workspace_scope_id: str = "workspace-product-eng") -> dict:
    return {
        "session_id": session_id, "turn_id": turn_id, "product_id": "linear-demo",
        "message": "show me the issues", "workspace_scope_id": workspace_scope_id,
    }


if __name__ == "__main__":
    unittest.main()
