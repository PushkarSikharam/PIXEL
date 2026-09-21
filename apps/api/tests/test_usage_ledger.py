"""Milestone 2: organization- and product-scoped provider usage ledger. Fake providers only; no network calls."""
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
from app.auth import AuthUser, create_token, create_visitor_token
from app.definitions.access import authorize_product
from app.definitions.organizations import OrganizationDirectory
from app.main import app
from app.services import env as env_module
from app.services import usage_ledger as ledger_module
from app.services.agent_reasoner import AgentReasoner, AgentReasoningContext
from app.services.product_data_store import ProductDataStore
from app.services.usage_ledger import AccountingUnavailable, AttemptRequest, BudgetExceeded, UsageLedger
from app.tenancy import ProductContext
from app.workspace_config import get_workspace_scope

SECRET_MESSAGE = "please keep this visitor sentence out of logs"
DEMO_PRODUCT = ProductContext(tenant_id="pixel-dev", team_id="planning-team", product_id="linear-demo",
                              deployment_id="local-dev")
OTHER_TENANT = ProductContext(tenant_id="acme", team_id="billing", product_id="acme-billing", deployment_id="local-dev")


ORG_ADMIN = AuthUser(kind="member", user_id="demo-admin", tenant_id="pixel-dev", role="org_admin")


class LedgerFixture(unittest.TestCase):
    """Temporary database and an environment isolated from developer settings."""

    def setUp(self):
        files_patch = patch.object(env_module, "_env_files", lambda: ())
        files_patch.start()
        self.addCleanup(files_patch.stop)
        env_patch = patch.dict(os.environ, {"PIXEL_BLOCK_EXTERNAL_HTTP": "true"})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for name in [key for key in os.environ if key.startswith(("PIXEL_BUDGET_", "PIXEL_TOTAL_", "PIXEL_DEPLOYMENT",
                                                                  "PIXEL_DEMO_SEEDS", "LLM_"))]:
            os.environ.pop(name)
        os.environ.pop("PIXEL_PAID_PROVIDERS_ENABLED", None)
        os.environ["PIXEL_DEMO_SEEDS"] = "true"

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        db_patch = patch.object(db, "DB_PATH", Path(temporary.name) / "usage.sqlite3")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        db.migrate()
        self.ledger = UsageLedger()
        self.tenant = DEMO_PRODUCT

    def attempt(self, tenant=None, **overrides) -> AttemptRequest:
        fields = {
            "tenant": tenant or self.tenant,
            "user_id": "demo-product-eng",
            "request_id": "req-1",
            "capability": "speech",
            "provider": "azure",
            "reserved_units": 10,
            **overrides,
        }
        return AttemptRequest(**fields)

    def reserve(self, tenant=None, **overrides) -> str:
        return self.ledger.reserve(self.attempt(tenant, **overrides))

    def rows(self) -> list[sqlite3.Row]:
        with db.get_connection() as connection:
            return connection.execute("select * from provider_attempts order by created_at").fetchall()

    def reasoning_context(self) -> AgentReasoningContext:
        data = ProductDataStore().load()  # the caller's records; there is no shared default
        return AgentReasoningContext(
            owner=self.tenant,
            definition_id="linear_simplified",
            message=SECRET_MESSAGE,
            current_page="dashboard",
            selected_issue_id=None,
            workspace_scope=get_workspace_scope("workspace-product-eng", data),
            visible_data=data,
            retrieved_docs=[],
            user_id="demo-product-eng",
            session_id="session-1",
            request_id="turn:session-1:1",
        )

    def statuses(self) -> list[str]:
        return [row["status"] for row in self.rows()]


class UsageLedgerTest(LedgerFixture):
    # --- Ownership ---

    def test_every_attempt_records_its_owner(self):
        self.reserve(session_id="session-1", model="en-US-Ava")
        row = self.rows()[0]
        self.assertEqual(
            (row["tenant_id"], row["team_id"], row["product_id"], row["deployment_id"]),
            ("pixel-dev", "planning-team", "linear-demo", "local-dev"),
        )
        self.assertEqual((row["user_id"], row["session_id"]), ("demo-product-eng", "session-1"))
        self.assertEqual((row["unit"], row["reserved_units"], row["status"]), ("characters", 10, "reserved"))

    def test_team_attribution_is_recorded_at_attempt_time(self):
        directory = OrganizationDirectory()
        self.reserve(authorize_product(ORG_ADMIN, "linear-demo", directory).context, request_id="before")
        directory.create_team("pixel-dev", "growth-team", "Growth")
        directory.transfer_product("pixel-dev", "linear-demo", "growth-team")
        self.reserve(authorize_product(ORG_ADMIN, "linear-demo", directory).context, request_id="after")
        # Earlier attempts keep the team that owned the product when they were made.
        self.assertEqual([row["team_id"] for row in self.rows()], ["planning-team", "growth-team"])
        summary = self.ledger.organization_summary("pixel-dev", "local-dev")
        self.assertEqual(
            [(row["team_id"], row["product_id"], row["attempts"]) for row in summary],
            [("growth-team", "linear-demo", 1), ("planning-team", "linear-demo", 1)],
        )

    def test_organization_identity_is_independent_of_the_deployment(self):
        os.environ["PIXEL_DEPLOYMENT_ID"] = "pixel-prod-us"
        context = authorize_product(ORG_ADMIN, "linear-demo").context
        self.assertEqual((context.tenant_id, context.deployment_id), ("pixel-dev", "pixel-prod-us"))

    def test_tenant_budgets_are_independent(self):
        os.environ["PIXEL_BUDGET_SPEECH_DEPLOYMENT_ATTEMPTS"] = "1"
        self.reserve(request_id="a")
        with self.assertRaises(BudgetExceeded):
            self.reserve(request_id="b")
        self.reserve(OTHER_TENANT, request_id="b")

    def test_summary_only_contains_the_requested_tenant(self):
        self.reserve(request_id="a")
        self.reserve(OTHER_TENANT, request_id="b", reserved_units=99)
        mine = self.ledger.summary(self.tenant)
        theirs = self.ledger.summary(OTHER_TENANT)
        self.assertEqual([(row["attempts"], row["reserved_units"]) for row in mine], [(1, 10)])
        self.assertEqual([(row["attempts"], row["reserved_units"]) for row in theirs], [(1, 99)])

    def test_attempts_cannot_be_settled_or_released_across_tenants(self):
        attempt_id = self.reserve()
        self.assertFalse(self.ledger.settle(attempt_id, OTHER_TENANT, "succeeded"))
        self.assertFalse(self.ledger.release(attempt_id, OTHER_TENANT))
        self.assertTrue(self.ledger.settle(attempt_id, self.tenant, "succeeded"))

    # --- Reservation rules ---

    def test_concurrent_reservations_never_exceed_the_limit(self):
        os.environ["PIXEL_BUDGET_SPEECH_DEPLOYMENT_ATTEMPTS"] = "3"

        def attempt(index: int) -> str:
            try:
                self.reserve(request_id=f"req-{index}", user_id=f"user-{index}")
                return "reserved"
            except BudgetExceeded:
                return "blocked"

        with ThreadPoolExecutor(max_workers=10) as pool:
            outcomes = list(pool.map(attempt, range(10)))
        self.assertEqual(outcomes.count("reserved"), 3)
        self.assertEqual(outcomes.count("blocked"), 7)

    def test_budget_survives_restart_and_demo_reset(self):
        os.environ["PIXEL_BUDGET_SPEECH_USER_ATTEMPTS"] = "2"
        self.reserve(request_id="a")
        self.reserve(request_id="b")
        ProductDataStore().reset()
        db.migrate()
        with self.assertRaises(BudgetExceeded) as refusal:
            UsageLedger().reserve(self.attempt(request_id="c"))
        self.assertEqual(refusal.exception.reason, "user_attempt_limit")

    def test_new_request_or_session_ids_do_not_bypass_user_budget(self):
        os.environ["PIXEL_BUDGET_SPEECH_USER_ATTEMPTS"] = "1"
        self.reserve(request_id="first", session_id="s1")
        with self.assertRaises(BudgetExceeded):
            self.reserve(request_id="second", session_id="s2")
        self.reserve(request_id="third", user_id="demo-platform")

    def test_session_attempt_limit_is_persistent(self):
        os.environ["PIXEL_BUDGET_REASONING_SESSION_ATTEMPTS"] = "1"
        reasoning = {"capability": "reasoning", "provider": "gemini", "session_id": "s1"}
        self.reserve(request_id="turn-1", **reasoning)
        with self.assertRaises(BudgetExceeded) as refusal:
            UsageLedger().reserve(self.attempt(request_id="turn-2", **reasoning))
        self.assertEqual(refusal.exception.reason, "session_attempt_limit")
        self.reserve(request_id="turn-3", **{**reasoning, "session_id": "s2"})

    def test_each_fallback_attempt_counts_and_request_is_capped_at_two(self):
        first = self.reserve(provider="azure", reserved_units=100)
        self.ledger.settle(first, self.tenant, "failed")
        second = self.reserve(provider="gemini", reserved_units=100)
        self.ledger.settle(second, self.tenant, "succeeded")
        with self.assertRaises(BudgetExceeded) as refusal:
            self.reserve(provider="openai", reserved_units=100)
        self.assertEqual(refusal.exception.reason, "request_attempt_limit")
        self.assertEqual(sum(row["reserved_units"] for row in self.rows()), 200)

    def test_unit_budget_counts_every_attempt(self):
        os.environ["PIXEL_BUDGET_SPEECH_USER_UNITS"] = "150"
        self.reserve(request_id="a", reserved_units=100)
        with self.assertRaises(BudgetExceeded) as refusal:
            self.reserve(request_id="b", reserved_units=100)
        self.assertEqual(refusal.exception.reason, "user_unit_limit")

    def test_reported_usage_above_the_estimate_is_what_counts(self):
        os.environ["PIXEL_BUDGET_REASONING_USER_UNITS"] = "5000"
        reasoning = {"capability": "reasoning", "provider": "gemini"}
        attempt_id = self.reserve(request_id="a", reserved_units=1000, **reasoning)
        self.ledger.settle(attempt_id, self.tenant, "succeeded", actual_units=4500)
        with self.assertRaises(BudgetExceeded) as refusal:
            self.reserve(request_id="b", reserved_units=1000, **reasoning)
        self.assertEqual(refusal.exception.reason, "user_unit_limit")

    def test_oversized_attempts_are_blocked_before_any_dispatch(self):
        with self.assertRaises(BudgetExceeded) as refusal:
            self.reserve(reserved_units=2001)
        self.assertEqual(refusal.exception.reason, "attempt_too_large")
        with self.assertRaises(BudgetExceeded):
            self.reserve(request_id="r", capability="reasoning", provider="gemini", reserved_units=4513)
        self.assertEqual(self.statuses(), ["blocked", "blocked"])

    def test_realtime_is_always_blocked(self):
        with self.assertRaises(BudgetExceeded) as refusal:
            self.reserve(capability="realtime", provider="openai", reserved_units=0)
        self.assertEqual(refusal.exception.reason, "capability_disabled")

    def test_kill_switch_blocks_every_capability(self):
        os.environ["PIXEL_PAID_PROVIDERS_ENABLED"] = "false"
        for capability in ("speech", "reasoning"):
            with self.assertRaises(BudgetExceeded) as refusal:
                self.reserve(request_id=capability, capability=capability)
            self.assertEqual(refusal.exception.reason, "providers_disabled")
        # Blocked rows still say what they would have measured, and reserve nothing.
        self.assertEqual([(row["unit"], row["reserved_units"]) for row in self.rows()],
                         [("characters", 0), ("tokens", 0)])

    def test_total_attempt_cap_is_authoritative(self):
        os.environ["PIXEL_TOTAL_ATTEMPT_CAP"] = "2"
        self.reserve(request_id="a")
        self.reserve(request_id="b", capability="reasoning", provider="gemini")
        with self.assertRaises(BudgetExceeded) as refusal:
            self.reserve(request_id="c")
        self.assertEqual(refusal.exception.reason, "total_attempt_cap")

    def test_blocked_attempts_do_not_consume_allowance(self):
        os.environ["PIXEL_BUDGET_SPEECH_USER_ATTEMPTS"] = "1"
        with self.assertRaises(BudgetExceeded):
            self.reserve(request_id="too-long", reserved_units=5000)
        self.reserve(request_id="ok")

    # --- Settling and releasing ---

    def test_timeouts_failures_and_cancellations_keep_allowance_consumed(self):
        os.environ["PIXEL_BUDGET_SPEECH_USER_ATTEMPTS"] = "3"
        for index, status in enumerate(("timeout", "failed", "cancelled")):
            attempt_id = self.reserve(request_id=f"req-{index}")
            self.assertTrue(self.ledger.settle(attempt_id, self.tenant, status))
        with self.assertRaises(BudgetExceeded):
            self.reserve(request_id="req-4")

    def test_release_returns_allowance_only_for_unsent_reservations(self):
        os.environ["PIXEL_BUDGET_SPEECH_USER_ATTEMPTS"] = "1"
        unsent = self.reserve(request_id="a")
        self.assertTrue(self.ledger.release(unsent, self.tenant))
        sent = self.reserve(request_id="b")
        self.ledger.settle(sent, self.tenant, "timeout")
        self.assertFalse(self.ledger.release(sent, self.tenant))
        with self.assertRaises(BudgetExceeded):
            self.reserve(request_id="c")

    def test_settle_is_single_use(self):
        attempt_id = self.reserve()
        self.assertTrue(self.ledger.settle(attempt_id, self.tenant, "succeeded"))
        self.assertFalse(self.ledger.settle(attempt_id, self.tenant, "failed"))

    def test_accounting_failure_fails_closed(self):
        def broken_connection():
            raise sqlite3.OperationalError("disk I/O error")

        with patch.object(ledger_module, "get_connection", broken_connection):
            with self.assertRaises(AccountingUnavailable):
                self.reserve()

    def test_expired_rows_are_pruned(self):
        attempt_id = self.reserve()
        with db.get_connection() as connection:
            connection.execute(
                "update provider_attempts set usage_day = '2020-01-01' where attempt_id = ?", (attempt_id,)
            )
        db.migrate()
        self.assertEqual(self.rows(), [])

    def test_unowned_draft_table_is_replaced(self):
        with db.get_connection() as connection:
            connection.executescript(
                "drop table provider_attempts;"
                "create table provider_attempts(id text primary key, reserved_chars integer);"
            )
        db.migrate()
        self.reserve()
        self.assertEqual(self.rows()[0]["tenant_id"], "pixel-dev")

    def test_logs_contain_metadata_only(self):
        with self.assertLogs("pixel.usage", level="INFO") as logs:
            attempt_id = self.reserve(request_id="req-log")
            self.ledger.settle(attempt_id, self.tenant, "succeeded", duration_ms=12)
        output = "\n".join(logs.output)
        self.assertIn('"tenant_id": "pixel-dev"', output)
        self.assertIn("attempt_settled", output)
        self.assertNotIn(SECRET_MESSAGE, output)

    # --- Reasoning: FastAPI reserves before each Gemini attempt ---


    def fake_reasoner(self, response=None, error=None):
        calls = []

        def transport(api_key, payload, timeout_ms):
            calls.append(payload)
            if error:
                raise error
            return response

        os.environ.update({"LLM_ENABLED": "true", "LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "fake-key"})
        return AgentReasoner(transport=transport), calls

    def test_reasoning_records_provider_reported_tokens(self):
        response = {
            "candidates": [{"content": {"parts": [{"text": (
                '{"speech": "I will open issues.", "proposed_action": null, '
                '"clarification_question": null, "intent_trace": {"confidence": 0.8, "status": "active"}}'
            )}]}}],
            "usageMetadata": {"promptTokenCount": 120, "candidatesTokenCount": 40, "thoughtsTokenCount": 5},
        }
        reasoner, calls = self.fake_reasoner(response=response)
        with self.assertLogs("pixel.usage", level="INFO") as logs:
            result = reasoner.reason(self.reasoning_context())
        self.assertIsNotNone(result)
        self.assertEqual(len(calls), 1)
        row = self.rows()[0]
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual((row["actual_input_units"], row["actual_output_units"], row["actual_units"]), (120, 45, 165))
        self.assertEqual((row["capability"], row["unit"], row["session_id"]), ("reasoning", "tokens", "session-1"))
        self.assertGreater(row["reserved_units"], 512)
        self.assertNotIn(SECRET_MESSAGE, "\n".join(logs.output))
        self.assertNotIn("fake-key", "\n".join(logs.output))

    def test_reasoning_over_budget_never_calls_the_provider(self):
        os.environ["PIXEL_BUDGET_REASONING_USER_ATTEMPTS"] = "1"
        reasoner, calls = self.fake_reasoner(error=TimeoutError())
        self.assertIsNone(reasoner.reason(self.reasoning_context()))
        self.assertIsNone(reasoner.reason(self.reasoning_context()))
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.statuses(), ["timeout", "blocked"])

    def test_reasoning_with_accounting_down_never_calls_the_provider(self):
        reasoner, calls = self.fake_reasoner(response={})

        def broken_connection():
            raise sqlite3.OperationalError("database is locked")

        with patch.object(ledger_module, "get_connection", broken_connection):
            self.assertIsNone(reasoner.reason(self.reasoning_context()))
        self.assertEqual(calls, [])


class UsageApiTest(LedgerFixture):
    """Organization binding of tokens and the organization-scoped admin usage summary."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(app, raise_server_exceptions=False)

    def headers(self, user_id="demo-product-eng") -> dict[str, str]:
        return {"Authorization": f"Bearer {create_token(user_id)}"}

    def test_tokens_require_an_active_organization_membership(self):
        with self.assertRaises(ValueError):
            create_token("demo-admin", tenant_id="acme")
        headers = self.headers()
        self.assertEqual(self.client.get("/api/demo-data", headers=headers).status_code, 200)
        directory = OrganizationDirectory()
        directory.set_organization_state("pixel-dev", "suspended")
        self.assertEqual(self.client.get("/api/demo-data", headers=headers).status_code, 403)
        directory.set_organization_state("pixel-dev", "active")
        with db.get_connection() as connection:
            connection.execute("delete from memberships where user_id = 'demo-product-eng'")
        self.assertEqual(self.client.get("/api/demo-data", headers=headers).status_code, 401)

    def test_reservation_endpoints_no_longer_exist(self):
        response = self.client.post("/api/usage/reservations", json={}, headers=self.headers())
        self.assertEqual(response.status_code, 404)

    def test_summary_is_for_organization_admins_and_their_organization_only(self):
        self.reserve(request_id="mine")
        self.reserve(OTHER_TENANT, request_id="foreign")
        self.assertEqual(self.client.get("/api/usage/summary", headers=self.headers()).status_code, 403)
        visitor_token, _ = create_visitor_token("pixel-dev", "linear-demo")
        visitor = {"Authorization": f"Bearer {visitor_token}"}
        self.assertEqual(self.client.get("/api/usage/summary", headers=visitor).status_code, 403)
        summary = self.client.get("/api/usage/summary", headers=self.headers("demo-admin")).json()
        self.assertEqual((summary["tenant_id"], summary["deployment_id"]), ("pixel-dev", "local-dev"))
        self.assertEqual(
            [(row["team_id"], row["product_id"], row["attempts"]) for row in summary["rows"]],
            [("planning-team", "linear-demo", 1)],
        )


if __name__ == "__main__":
    unittest.main()
