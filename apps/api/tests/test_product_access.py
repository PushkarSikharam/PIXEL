"""Milestone 3.1: product isolation through the real turn path and HTTP API.

The seeded development organization `pixel-dev` owns product `linear-demo` (team
`planning-team`). Each test adds the other organizations, teams and products it needs.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app import db
from app.auth import AuthUser, create_token
from app.definitions.organizations import OrganizationDirectory
from app.main import app
from app.schemas import TurnRequest
from app.services import env as env_module
from app.services.agent import DemoAgent
from app.services.product_data_store import ProductDataStore

DEFINITION_ID = "linear_simplified"
# The version the demo seed binds for new sessions.
SEEDED_VERSION = __import__("json").loads(
    (Path(__file__).resolve().parents[3] / "products" / DEFINITION_ID / "seed" / "demo_organization.json")
    .read_text(encoding="utf-8"))["product"]["definition_version"]
ADMIN = AuthUser(kind="member", user_id="demo-admin", tenant_id="pixel-dev", role="org_admin")
PLANNER = AuthUser(kind="member", user_id="demo-product-eng", tenant_id="pixel-dev", role="team_member",
                   team_id="planning-team")


def visitor(product_id: str, tenant_id: str = "pixel-dev") -> AuthUser:
    return AuthUser(kind="visitor", user_id="visitor-1", tenant_id=tenant_id, product_id=product_id)


def turn(session_id: str, product_id: str, turn_id: int = 1, message: str = "Open the issues page.") -> TurnRequest:
    return TurnRequest(session_id=session_id, turn_id=turn_id, product_id=product_id, message=message,
                       input_mode="text", workspace_scope_id="workspace-product-eng")


class ProductAccessFixture(unittest.TestCase):
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
        db_patch = patch.object(db, "DB_PATH", Path(temporary.name) / "access.sqlite3")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        db.migrate()

        self.directory = OrganizationDirectory()
        self.directory.create_team("pixel-dev", "support-team", "Support")
        self.directory.bind_product("pixel-dev", "support-desk", "support-team", DEFINITION_ID, 1,
                                    visitor_access=True)
        self.directory.create_organization("acme", "Acme")
        self.directory.create_team("acme", "planning-team", "Planning")
        self.directory.add_member("acme", "acme-admin", "org_admin")
        self.directory.bind_product("acme", "acme-demo", "planning-team", DEFINITION_ID, 1, visitor_access=True)
        self.agent = DemoAgent()

    def handle_turn(self, request: TurnRequest, principal: AuthUser):
        # The turn endpoint passes the caller's own records. These tests are about which
        # product a principal may use, so they pass the member demo records.
        return self.agent.handle_turn(request, principal, ProductDataStore().load())

    def assert_turn_denied(self, principal: AuthUser, request: TurnRequest, reason: str):
        response = self.handle_turn(request, principal)
        self.assertEqual(response.status, "denied")
        self.assertIn(reason, response.intent_trace.reason)
        self.assertIsNone(response.validated_action)
        return response


class TurnIsolationTest(ProductAccessFixture):
    def test_the_seeded_product_runs_on_its_pinned_definition(self):
        response = self.handle_turn(turn("s1", "linear-demo"), PLANNER)
        self.assertEqual(response.status, "completed")
        self.assertEqual(response.validated_action.type, "OPEN_ISSUES")
        pin = self.agent.sessions.pin_for("s1")
        self.assertEqual(
            (pin.tenant_id, pin.team_id, pin.product_id, pin.definition_id, pin.definition_version),
            ("pixel-dev", "planning-team", "linear-demo", DEFINITION_ID, SEEDED_VERSION),
        )

    def test_same_organization_different_teams(self):
        self.assert_turn_denied(PLANNER, turn("s1", "support-desk"), "product_not_found")
        self.assertFalse(self.agent.sessions.exists("s1"), "a denied turn creates no session")
        self.assertEqual(self.handle_turn(turn("s2", "support-desk"), ADMIN).status, "completed")

    def test_same_organization_different_products(self):
        self.handle_turn(turn("s1", "linear-demo"), ADMIN)
        response = self.assert_turn_denied(ADMIN, turn("s1", "support-desk", turn_id=2), "owned by another")
        self.assertIn("belongs to someone else", response.speech)
        self.assertEqual(self.agent.sessions.pin_for("s1").product_id, "linear-demo")

    def test_different_organizations(self):
        acme_admin = AuthUser(kind="member", user_id="acme-admin", tenant_id="acme", role="org_admin")
        self.assert_turn_denied(ADMIN, turn("s1", "acme-demo"), "product_not_found")
        self.assertEqual(self.handle_turn(turn("acme-session", "acme-demo"), acme_admin).status, "completed")
        # Another organization cannot continue the session, even through its own product of the same name.
        self.directory.create_team("pixel-dev", "acme-lookalike", "Lookalike")
        self.directory.bind_product("pixel-dev", "acme-demo", "acme-lookalike", DEFINITION_ID, 1)
        self.assert_turn_denied(ADMIN, turn("acme-session", "acme-demo", turn_id=2), "owned by another")

    def test_disabled_products_end_live_sessions(self):
        self.handle_turn(turn("s1", "linear-demo"), PLANNER)
        self.directory.set_product_state("pixel-dev", "linear-demo", "disabled")
        self.assert_turn_denied(PLANNER, turn("s1", "linear-demo", turn_id=2), "product_disabled")
        self.assert_turn_denied(PLANNER, turn("s2", "linear-demo"), "product_disabled")
        self.assertEqual(self.handle_turn(turn("s3", "support-desk"), ADMIN).status, "completed")

    def test_visitors_are_pinned_to_their_product(self):
        guest = visitor("support-desk")
        self.assertEqual(self.handle_turn(turn("guest", "support-desk"), guest).status, "completed")
        self.assert_turn_denied(guest, turn("guest", "linear-demo", turn_id=2), "product_not_found")
        self.assert_turn_denied(guest, turn("guest-2", "linear-demo"), "product_not_found")
        self.assert_turn_denied(visitor("acme-demo"), turn("guest-3", "acme-demo"), "product_not_found")

    def test_transferred_products_end_sessions_of_the_old_team(self):
        self.handle_turn(turn("s1", "linear-demo"), ADMIN)
        self.directory.transfer_product("pixel-dev", "linear-demo", "support-team")
        response = self.assert_turn_denied(ADMIN, turn("s1", "linear-demo", turn_id=2), "product_transferred")
        self.assertIn("has ended", response.speech)
        self.assert_turn_denied(PLANNER, turn("s2", "linear-demo"), "product_not_found")


class ApiAccessTest(ProductAccessFixture):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app, raise_server_exceptions=False)

    def headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def start_visitor(self, tenant_id: str, product_id: str):
        return self.client.post(f"/api/organizations/{tenant_id}/products/{product_id}/visitor-sessions")

    def test_demo_login_resolves_the_organization_from_membership(self):
        body = self.client.post("/api/auth/demo-login", json={"user_id": "demo-product-eng"}).json()
        self.assertEqual((body["user_id"], body["tenant_id"]), ("demo-product-eng", "pixel-dev"))
        self.assertEqual(self.client.post("/api/auth/demo-login", json={"user_id": "nobody"}).status_code, 404)

    def test_visitor_sessions_are_issued_only_for_products_that_accept_visitors(self):
        started = self.start_visitor("pixel-dev", "support-desk")
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.json()["product_id"], "support-desk")
        self.directory.bind_product("pixel-dev", "internal-desk", "support-team", DEFINITION_ID, 1)
        self.directory.set_product_state("acme", "acme-demo", "disabled")
        refusals = [
            self.start_visitor("pixel-dev", "internal-desk"),
            self.start_visitor("pixel-dev", "missing-desk"),
            self.start_visitor("acme", "acme-demo"),
            self.start_visitor("nobody", "linear-demo"),
        ]
        # Unknown, private and disabled products are indistinguishable.
        self.assertEqual({(r.status_code, r.json()["detail"]) for r in refusals},
                         {(404, "This product is not available.")})

    def test_visitors_cannot_use_organization_or_product_data_endpoints(self):
        token = self.start_visitor("pixel-dev", "support-desk").json()["token"]
        for method, path in (("get", "/api/demo-data"), ("post", "/api/demo-data/reset"), ("get", "/api/usage/summary")):
            with self.subTest(path=path):
                response = getattr(self.client, method)(path, headers=self.headers(token))
                self.assertEqual(response.status_code, 403)

    def test_member_turns_are_limited_to_their_team_products(self):
        token = create_token("demo-product-eng")
        own = self.client.post("/api/turn", headers=self.headers(token),
                               json=turn("api-1", "linear-demo").model_dump())
        other = self.client.post("/api/turn", headers=self.headers(token),
                                 json=turn("api-2", "support-desk").model_dump())
        self.assertEqual(own.json()["status"], "completed")
        self.assertEqual(other.json()["status"], "denied")
        self.assertEqual(other.json()["speech"], "That product is not available to you.")

    def test_suspended_organizations_are_refused(self):
        token = create_token("acme-admin")
        self.directory.set_organization_state("acme", "suspended")
        response = self.client.post("/api/turn", headers=self.headers(token),
                                    json=turn("acme-1", "acme-demo").model_dump())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.start_visitor("acme", "acme-demo").status_code, 404)



class DemoSeedDefaultTest(unittest.TestCase):
    """Demo seeds fail safe: a missing setting never creates synthetic organizations."""

    def migrate(self, **env) -> OrganizationDirectory:
        files_patch = patch.object(env_module, "_env_files", lambda: ())
        env_patch = patch.dict(os.environ, {name: value for name, value in env.items() if value is not None})
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        db_patch = patch.object(db, "DB_PATH", Path(temporary) / "seeds.sqlite3")
        for active in (files_patch, env_patch, db_patch):
            self.enterContext(active)
        for name, value in env.items():
            if value is None:
                os.environ.pop(name, None)
        db.migrate()
        return OrganizationDirectory()

    def test_unset_setting_creates_no_demo_organization(self):
        directory = self.migrate(PIXEL_DEMO_SEEDS=None)
        self.assertIsNone(directory.organization("pixel-dev"))
        self.assertIsNone(directory.product("pixel-dev", "linear-demo"))
        self.assertIsNone(directory.membership("pixel-dev", "demo-admin"))

    def test_disabled_setting_creates_no_demo_organization(self):
        self.assertIsNone(self.migrate(PIXEL_DEMO_SEEDS="false").organization("pixel-dev"))

    def test_enabled_setting_creates_the_demo_organization(self):
        directory = self.migrate(PIXEL_DEMO_SEEDS="true")
        self.assertEqual(directory.organization("pixel-dev").state, "active")
        self.assertEqual(directory.product("pixel-dev", "linear-demo").team_id, "planning-team")
        self.assertEqual(directory.membership("pixel-dev", "demo-admin").role, "org_admin")


if __name__ == "__main__":
    unittest.main()
