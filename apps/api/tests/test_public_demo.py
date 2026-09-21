"""The public demo cannot hand out administration, and cannot be flooded.

Before this, the public web app signed every visitor in as `demo-admin`, and the demo login issued
a token for any user ID it was sent. So any visitor held an administrator's token, and the page's
own Reset button wiped everyone's demo data. These tests hold the fix in place.
"""
from __future__ import annotations

import io
import os
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

import app.main as main_module
from app import db, ops
from app.auth import create_token, demo_identity_allowed
from app.definitions.organizations import OrganizationDirectory
from app.main import app, product_data, readiness
from app.record_access import grant_records
from app.services import env as env_module
from app.services.rate_limit import TOO_MANY, Limit, RateLimiter

TENANT = "pixel-dev"
PRODUCT = "linear-demo"


class PublicDemoFixture(unittest.TestCase):
    extra_env: dict[str, str] = {}

    def setUp(self):
        files_patch = patch.object(env_module, "_env_files", lambda: ())
        files_patch.start()
        self.addCleanup(files_patch.stop)
        env = {"LLM_ENABLED": "false", "PIXEL_SYNTHETIC_DEMO": "true", "PIXEL_DEMO_SEEDS": "true",
               "PIXEL_PAID_PROVIDERS_ENABLED": "false", **self.extra_env}
        env_patch = patch.dict(os.environ, env)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for name in ("PIXEL_DEMO_ADMIN_LOGIN", "PIXEL_DEMO_LOGIN_USERS", "PIXEL_RATE_LIMITS"):
            if name not in self.extra_env:
                os.environ.pop(name, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        db_patch = patch.object(db, "DB_PATH", Path(temporary.name) / "public.sqlite3")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        db.migrate()
        product_data.seed_if_empty()
        readiness.invalidate()
        self.client = TestClient(app)

    def login(self, user_id: str, **headers):
        return self.client.post("/api/auth/demo-login", json={"user_id": user_id}, headers=headers)

    def token(self, user_id: str) -> dict[str, str]:
        response = self.login(user_id)
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": f"Bearer {response.json()['token']}"}


class DemoIdentityTest(PublicDemoFixture):
    def test_the_public_demo_identity_signs_in(self):
        self.assertEqual(self.login("demo-visitor").status_code, 200)

    def test_the_public_demo_identity_is_not_an_administrator(self):
        membership = OrganizationDirectory().membership(TENANT, "demo-visitor")
        self.assertEqual(membership.role, "team_member")

    def test_the_public_demo_identity_sees_both_demo_workspaces(self):
        """So the demo behaves as it did — both workspaces — without any administration."""
        data = self.client.get("/api/demo-data", headers=self.token("demo-visitor")).json()
        self.assertEqual(sorted(scope["id"] for scope in data["workspaceScopes"]),
                         ["workspace-platform", "workspace-product-eng"])

    def test_the_administrator_cannot_be_minted_through_the_demo(self):
        """The reproduction: anyone could send {"user_id": "demo-admin"}."""
        self.assertEqual(self.login("demo-admin").status_code, 404)

    def test_the_refusal_is_indistinguishable_from_an_unknown_user(self):
        """The endpoint never confirms that an administrator exists."""
        admin, unknown = self.login("demo-admin"), self.login("nobody-at-all")
        self.assertEqual((admin.status_code, admin.json()), (unknown.status_code, unknown.json()))

    def test_a_record_administrator_is_refused_even_without_an_admin_role(self):
        """Administration over the records is enough to reset everyone's data."""
        directory = OrganizationDirectory()
        directory.add_member(TENANT, "records-admin", "team_member", "planning-team")
        grant_records(TENANT, PRODUCT, "records-admin", ["workspace-product-eng"], is_admin=True)
        self.assertFalse(demo_identity_allowed("records-admin", TENANT))
        self.assertEqual(self.login("records-admin").status_code, 404)

    def test_an_ordinary_member_is_still_allowed(self):
        self.assertTrue(demo_identity_allowed("demo-product-eng", TENANT))


class IsolatedHarnessTest(PublicDemoFixture):
    extra_env = {"PIXEL_DEMO_ADMIN_LOGIN": "true"}

    def test_an_isolated_test_harness_may_sign_in_as_the_administrator(self):
        self.assertEqual(self.login("demo-admin").status_code, 200)


class LoginAllowlistTest(PublicDemoFixture):
    extra_env = {"PIXEL_DEMO_LOGIN_USERS": "demo-visitor"}

    def test_an_allowlist_narrows_the_demo_to_the_listed_users(self):
        self.assertEqual(self.login("demo-visitor").status_code, 200)
        self.assertEqual(self.login("demo-product-eng").status_code, 404)

    def test_an_allowlist_cannot_let_an_administrator_in(self):
        with patch.dict(os.environ, {"PIXEL_DEMO_LOGIN_USERS": "demo-visitor,demo-admin"}):
            self.assertEqual(self.login("demo-admin").status_code, 404)


class GlobalResetTest(PublicDemoFixture):
    def test_a_visitor_cannot_reset_everyones_data(self):
        """The reproduction: the public page's Reset button reset the shared demo."""
        response = self.client.post("/api/demo-data/reset", headers=self.token("demo-visitor"))
        self.assertEqual(response.status_code, 403)

    def test_an_administrator_still_can(self):
        headers = {"Authorization": f"Bearer {create_token('demo-admin', TENANT)}"}
        self.assertEqual(self.client.post("/api/demo-data/reset", headers=headers).status_code, 200)


class RateLimitTest(PublicDemoFixture):
    def limited(self, **limits: Limit) -> RateLimiter:
        mapping = {tuple(name.split("__")): limit for name, limit in limits.items()}
        limiter = RateLimiter(limits=mapping)
        limiter.arm()
        patcher = patch.object(main_module, "rate_limits", limiter)
        patcher.start()
        self.addCleanup(patcher.stop)
        return limiter

    def turn(self, headers, turn_id: int, client: str = "203.0.113.1"):
        return self.client.post("/api/turn", headers={**headers, "X-Forwarded-For": client}, json={
            "session_id": "rate-limit", "turn_id": turn_id, "product_id": PRODUCT,
            "message": "Show sprint planning", "input_mode": "text", "current_page": "dashboard",
            "workspace_scope_id": "workspace-product-eng",
        })

    def test_login_is_limited_per_client(self):
        self.limited(login__client=Limit(2, 60))
        first = self.login("demo-visitor", **{"X-Forwarded-For": "198.51.100.7"})
        second = self.login("demo-visitor", **{"X-Forwarded-For": "198.51.100.7"})
        third = self.login("demo-visitor", **{"X-Forwarded-For": "198.51.100.7"})
        self.assertEqual([first.status_code, second.status_code, third.status_code], [200, 200, 429])
        self.assertIn("Retry-After", third.headers)
        self.assertGreaterEqual(int(third.headers["Retry-After"]), 1)

    def test_one_client_cannot_use_up_another_clients_allowance(self):
        self.limited(login__client=Limit(1, 60))
        self.assertEqual(self.login("demo-visitor", **{"X-Forwarded-For": "198.51.100.1"}).status_code, 200)
        self.assertEqual(self.login("demo-visitor", **{"X-Forwarded-For": "198.51.100.2"}).status_code, 200)
        self.assertEqual(self.login("demo-visitor", **{"X-Forwarded-For": "198.51.100.1"}).status_code, 429)

    def test_forging_the_client_address_does_not_escape_the_identity_ceiling(self):
        """X-Forwarded-For can be forged when the API is called directly; the identity cannot."""
        headers = self.token("demo-visitor")
        self.limited(turn__client=Limit(100, 60), turn__identity=Limit(2, 60))
        statuses = [self.turn(headers, index + 1, client=f"192.0.2.{index}").status_code
                    for index in range(3)]
        self.assertEqual(statuses, [200, 200, 429])

    def test_fresh_identities_and_client_addresses_still_hit_the_deployment_ceiling(self):
        limiter = RateLimiter(limits={
            ("turn", "client"): Limit(100, 60),
            ("turn", "identity"): Limit(100, 60),
            ("turn", "deployment"): Limit(2, 60),
        })
        limiter.arm()
        self.assertIsNone(limiter.retry_after("turn", client="a", identity="visitor-a"))
        self.assertIsNone(limiter.retry_after("turn", client="b", identity="visitor-b"))
        self.assertIsNotNone(limiter.retry_after("turn", client="c", identity="visitor-c"))

    def test_speech_is_limited(self):
        headers = self.token("demo-visitor")
        self.limited(speech__identity=Limit(1, 60))
        body = {"text": "Hello", "product_id": PRODUCT}
        first = self.client.post("/api/speech", json=body, headers=headers)
        second = self.client.post("/api/speech", json=body, headers=headers)
        # The speech service also answers 429 when paid providers are switched off, so the rate
        # limit is identified by its own message and Retry-After, not by the status code alone.
        self.assertNotEqual(first.json().get("detail"), TOO_MANY)
        self.assertEqual((second.status_code, second.json()["detail"]), (429, TOO_MANY))
        self.assertIn("Retry-After", second.headers)

    def test_reset_is_limited_even_for_an_administrator(self):
        headers = {"Authorization": f"Bearer {create_token('demo-admin', TENANT)}"}
        self.limited(reset__identity=Limit(1, 60))
        self.assertEqual(self.client.post("/api/demo-data/reset", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/api/demo-data/reset", headers=headers).status_code, 429)

    def test_every_shared_record_write_is_limited_before_it_mutates(self):
        headers = self.token("demo-visitor")
        data = self.client.get("/api/demo-data", headers=headers).json()
        limiter = self.limited(write__identity=Limit(1, 60))
        self.assertIsNone(limiter.retry_after("write", client=None, identity="demo-visitor"))

        requests = (
            ("post", "/api/demo-data/issues", data["issues"][0]),
            ("put", f"/api/demo-data/issues/{data['issues'][0]['id']}", data["issues"][0]),
            ("post", "/api/demo-data/projects?workspace_scope_id=workspace-product-eng",
             data["projects"][0]),
            ("post", "/api/demo-data/cycles", data["cycles"][0]),
            ("post", "/api/demo-data/team-members?workspace_scope_id=workspace-product-eng",
             data["team"][0]),
        )
        for method, path, payload in requests:
            with self.subTest(path=path):
                response = self.client.request(method, path, headers=headers, json=payload)
                self.assertEqual((response.status_code, response.json()["detail"]), (429, TOO_MANY))
                self.assertIn("Retry-After", response.headers)

    def test_a_refused_request_is_not_counted(self):
        """A visitor who waits the stated time gets through, however often they were refused."""
        clock = [0.0]
        limiter = RateLimiter(limits={("login", "client"): Limit(1, 60)}, clock=lambda: clock[0])
        limiter.arm()
        self.assertIsNone(limiter.retry_after("login", client="a", identity=None))
        for _ in range(5):
            self.assertIsNotNone(limiter.retry_after("login", client="a", identity=None))
        clock[0] = 61.0
        self.assertIsNone(limiter.retry_after("login", client="a", identity=None))

    def test_limits_are_armed_by_server_startup_and_can_be_switched_off(self):
        limiter = RateLimiter(limits={("login", "client"): Limit(1, 60)})
        self.assertFalse(limiter.armed, "a limiter the server never started throttles nothing")
        limiter.arm()
        self.assertTrue(limiter.armed)
        with patch.dict(os.environ, {"PIXEL_RATE_LIMITS": "off"}):
            limiter.arm()
            self.assertFalse(limiter.armed)

    def test_the_running_server_arms_its_limits(self):
        main_module.rate_limits.disarm()
        self.addCleanup(main_module.rate_limits.disarm)
        with TestClient(app):
            self.assertTrue(main_module.rate_limits.armed)
        self.assertFalse(main_module.rate_limits.armed)

    def test_idle_visitors_are_forgotten(self):
        clock = [0.0]
        limiter = RateLimiter(limits={("login", "client"): Limit(5, 60)}, clock=lambda: clock[0])
        limiter.arm()
        for index in range(50):
            limiter.retry_after("login", client=f"visitor-{index}", identity=None)
        clock[0] = 120.0
        limiter.retry_after("login", client="late", identity=None)
        self.assertEqual(len(limiter._hits), 1)


class OperatorCommandTest(PublicDemoFixture):
    def run_command(self, name: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = ops.main([name])
        return code, output.getvalue()

    def test_reset_restores_the_seeded_demo(self):
        headers = self.token("demo-visitor")
        issue = self.client.get("/api/demo-data", headers=headers).json()["issues"][0]
        product_data.update_issue(issue["id"], {**issue, "title": "Changed by a visitor"})
        code, printed = self.run_command("reset-demo-data")
        self.assertEqual(code, 0)
        self.assertIn('"reset": true', printed)
        restored = {i["id"]: i for i in self.client.get("/api/demo-data", headers=headers).json()["issues"]}
        self.assertNotEqual(restored[issue["id"]]["title"], "Changed by a visitor")

    def test_readiness_passes_on_a_healthy_deployment(self):
        code, printed = self.run_command("check-readiness")
        self.assertEqual(code, 0)
        self.assertIn('"ready": true', printed)

    def test_readiness_fails_and_names_the_product_on_the_production_fault(self):
        with db.get_connection() as connection:
            connection.execute("update definition_versions set checksum = ?", ("0" * 64,))
            connection.execute("update product_bindings set definition_checksum = ?", ("0" * 64,))
        code, printed = self.run_command("check-readiness")
        self.assertEqual(code, 1)
        self.assertIn("definition_invalid", printed)
        self.assertIn("linear-demo", printed, "the operator gets the detail the public endpoint withholds")


if __name__ == "__main__":
    unittest.main()
