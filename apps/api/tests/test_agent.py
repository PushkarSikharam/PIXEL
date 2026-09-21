from __future__ import annotations

import tempfile
import os
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

from fastapi.testclient import TestClient

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

from app import db
from app.auth import AuthUser, create_token
from app.main import app
from app.schemas import IntentTrace, TurnRequest
from app.services.agent import DemoAgent
from app.services.product_data_store import ProductDataStore
from app.services.agent_reasoner import AgentReasoningResult, ReasonedAction
from app.services.session_manager import SessionManager

DEMO_ADMIN = AuthUser(kind="member", user_id="demo-admin", tenant_id="pixel-dev", role="org_admin")


class AuthenticatedTestClient:
    """Wraps TestClient to inject auth headers into every request."""

    def __init__(self, client: TestClient, token: str) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {token}"}

    def _merge(self, kwargs: dict) -> dict:
        headers = {**self._headers, **(kwargs.pop("headers", None) or {})}
        kwargs["headers"] = headers
        return kwargs

    def get(self, url, **kwargs):
        return self._client.get(url, **self._merge(kwargs))

    def post(self, url, **kwargs):
        return self._client.post(url, **self._merge(kwargs))

    def put(self, url, **kwargs):
        return self._client.put(url, **self._merge(kwargs))

    def delete(self, url, **kwargs):
        return self._client.delete(url, **self._merge(kwargs))


class FakeGeminiReasoner:
    def __init__(self, result: AgentReasoningResult) -> None:
        self.result = result
        self.context_messages: list[str] = []
        self.call_count = 0

    def enabled(self) -> bool:
        return True

    def reason(self, context):
        self.call_count += 1
        self.context_messages.append(context.message)
        return self.result


class AgentApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.env_patch = patch.dict(os.environ, {"LLM_ENABLED": "false", "PIXEL_DEMO_SEEDS": "true"})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        original_db_path = db.DB_PATH
        self.addCleanup(setattr, db, "DB_PATH", original_db_path)
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "test.sqlite3"
        db.migrate()
        token = create_token("demo-admin")
        self.client = AuthenticatedTestClient(TestClient(app), token)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sprint_planning_returns_cycles_action(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 1,
                "product_id": "linear-demo",
                "message": "We're using Jira and sprint planning is messy.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "OPEN_CYCLES")
        self.assertEqual(body["intent_trace"]["current_tool"], "Jira")
        self.assertEqual(body["intent_trace"]["relevant_feature"], "Cycles")
        self.assertEqual(body["retrieved_context"][0]["source"], "cycles.md")

    def test_out_of_domain_action_is_rejected(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 2,
                "product_id": "linear-demo",
                "message": "Open Salesforce and show me opportunities.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "denied")
        self.assertEqual(body["proposed_action"]["type"], "OPEN_SALESFORCE")
        self.assertIsNone(body["validated_action"])
        self.assertEqual(body["intent_trace"]["status"], "denied")
        self.assertEqual(body["retrieved_context"], [])

    def test_person_ticket_lookup_opens_specific_issue(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 4,
                "product_id": "linear-demo",
                "message": "Open the ticket created for Maya.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "OPEN_DEMO_ISSUE")
        self.assertEqual(body["validated_action"]["payload"]["issue_id"], "LIN-142")
        self.assertEqual(body["intent_trace"]["current_intent"], "Open specific issue")
        self.assertEqual(body["retrieved_context"][0]["source"], "issues.md")
        self.assertEqual(body["speech"], "I found LIN-142, assigned to Maya Chen. I'll open that ticket.")

    def test_platform_scope_blocks_product_engineering_issue(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_scope",
                "turn_id": 1,
                "product_id": "linear-demo",
                "workspace_scope_id": "workspace-platform",
                "message": "Open the ticket created for Maya.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "denied")
        self.assertEqual(body["proposed_action"]["type"], "OPEN_DEMO_ISSUE")
        self.assertIsNone(body["validated_action"])
        self.assertIn("Maya Chen is outside Platform Workspace", body["speech"])
        self.assertEqual(body["retrieved_context"], [])

    def test_platform_scope_allows_platform_issue(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_scope",
                "turn_id": 2,
                "product_id": "linear-demo",
                "workspace_scope_id": "workspace-platform",
                "message": "Open Avery's ticket.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "OPEN_DEMO_ISSUE")
        self.assertEqual(body["validated_action"]["payload"]["issue_id"], "LIN-131")
        self.assertEqual(body["speech"], "I found LIN-131, assigned to Avery Brooks. I'll open that ticket.")

    def test_product_scope_blocks_platform_selected_issue_update(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_scope",
                "turn_id": 3,
                "product_id": "linear-demo",
                "workspace_scope_id": "workspace-product-eng",
                "message": "Assign it to Noah.",
                "input_mode": "text",
                "current_page": "issue_detail",
                "selected_issue_id": "LIN-131",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "denied")
        self.assertEqual(body["proposed_action"]["type"], "UPDATE_DEMO_ISSUE")
        self.assertIsNone(body["validated_action"])
        self.assertIn("outside Product Engineering Workspace", body["speech"])

    def test_platform_scope_creates_issue_inside_platform_project(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_scope",
                "turn_id": 4,
                "product_id": "linear-demo",
                "workspace_scope_id": "workspace-platform",
                "message": "Create a ticket for Avery about migration readiness.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "CREATE_DEMO_ISSUE")
        self.assertEqual(body["validated_action"]["payload"]["assignee"], "Avery Brooks")
        self.assertIn(body["validated_action"]["payload"]["project"], {"Migration", "Planning"})

    def test_demo_data_persists_created_records_and_resets(self) -> None:
        created_issue = {
            "id": "PIX-900",
            "title": "Persist created ticket",
            "priority": "High",
            "assignee": "Maya Chen",
            "project": "Issue Triage",
            "projectId": "PRJ-102",
            "status": "Todo",
            "cycle": "Product Engineering Cycle 14",
            "estimate": "2 pts",
            "label": "Demo",
            "description": "Created by persistence test.",
        }

        create_response = self.client.post("/api/demo-data/issues", json=created_issue)
        load_response = self.client.get("/api/demo-data")

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(load_response.status_code, 200)
        loaded_issues = load_response.json()["issues"]
        self.assertTrue(any(issue["id"] == "PIX-900" for issue in loaded_issues))

        reset_response = self.client.post("/api/demo-data/reset")

        self.assertEqual(reset_response.status_code, 200)
        reset_issues = reset_response.json()["issues"]
        self.assertFalse(any(issue["id"] == "PIX-900" for issue in reset_issues))

    def test_demo_data_persists_project_scope_additions(self) -> None:
        created_project = {
            "id": "PRJ-900",
            "name": "Billing Workflow",
            "description": "Demo project created during review.",
            "progress": 10,
            "status": "Planned",
            "lead": "Maya Chen",
            "team": "Product Engineering",
            "targetDate": "2026-12-01",
        }

        response = self.client.post(
            "/api/demo-data/projects?workspace_scope_id=workspace-product-eng",
            json=created_project,
        )
        data_response = self.client.get("/api/demo-data")

        self.assertEqual(response.status_code, 200)
        product_scope = next(
            scope
            for scope in data_response.json()["workspaceScopes"]
            if scope["id"] == "workspace-product-eng"
        )
        self.assertIn("PRJ-900", product_scope["allowedProjectIds"])
        self.assertIn("Billing Workflow", product_scope["allowedIssueProjects"])

    def test_agent_finds_persisted_issue_by_assignee(self) -> None:
        self.client.post(
            "/api/demo-data/issues",
            json={
                "id": "PIX-901",
                "title": "Persisted Maya follow-up",
                "priority": "Medium",
                "assignee": "Maya Chen",
                "project": "Issue Triage",
                "projectId": "PRJ-102",
                "status": "Todo",
            },
        )

        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_persisted_issue",
                "turn_id": 1,
                "product_id": "linear-demo",
                "message": "All tickets for Maya",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "FILTER_ISSUES_BY_ASSIGNEE")
        self.assertIn("PIX-901", body["speech"])

    def test_validator_accepts_persisted_scoped_team_member(self) -> None:
        self.client.post(
            "/api/demo-data/team-members?workspace_scope_id=workspace-product-eng",
            json={
                "name": "Lucifer",
                "initials": "L",
                "role": "Product Engineer",
                "load": 50,
                "email": "lucifer@pixel.demo",
                "projectIds": ["PRJ-101"],
            },
        )

        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_persisted_member",
                "turn_id": 1,
                "product_id": "linear-demo",
                "message": "Create a ticket for Lucifer about GitHub onboarding",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "CREATE_DEMO_ISSUE")
        self.assertEqual(body["validated_action"]["payload"]["assignee"], "Lucifer")

    def test_misspelled_ticket_lookup_opens_specific_issue(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 12,
                "product_id": "linear-demo",
                "message": "Open the tikit for Maya.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "OPEN_DEMO_ISSUE")
        self.assertEqual(body["validated_action"]["payload"]["issue_id"], "LIN-142")
        self.assertEqual(body["retrieved_context"][0]["source"], "issues.md")

    def test_all_tickets_for_person_filters_issues(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 17,
                "product_id": "linear-demo",
                "message": "All the tickets for Maya which are assigned to her.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "FILTER_ISSUES_BY_ASSIGNEE")
        self.assertEqual(body["validated_action"]["payload"]["assignee"], "Maya Chen")
        self.assertIn("I found 1 ticket assigned to Maya Chen: LIN-142", body["speech"])
        self.assertEqual(body["session_summary"]["last_person"], "Maya Chen")
        self.assertIn("issues", body["session_summary"]["interests"])

    def test_issue_followup_uses_previous_issue_context(self) -> None:
        self.client.post(
            "/api/turn",
            json={
                "session_id": "session_followup",
                "turn_id": 1,
                "product_id": "linear-demo",
                "message": "All tickets for Maya.",
                "input_mode": "text",
            },
        )
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_followup",
                "turn_id": 2,
                "product_id": "linear-demo",
                "message": "What about Noah?",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "FILTER_ISSUES_BY_ASSIGNEE")
        self.assertEqual(body["validated_action"]["payload"]["assignee"], "Noah Patel")
        self.assertIn("Noah Patel", body["speech"])

    def test_vague_all_request_asks_clarifying_question(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 20,
                "product_id": "linear-demo",
                "message": "Open all the",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertIsNone(body["validated_action"])
        self.assertIn("Do you mean all issues", body["speech"])
        self.assertEqual(body["intent_trace"]["current_intent"], "Clarification needed")
        self.assertEqual(body["session_summary"]["clarification_pending"], "all_items")

    def test_vague_create_request_asks_targeted_clarification(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 24,
                "product_id": "linear-demo",
                "message": "Create something new.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertIsNone(body["proposed_action"])
        self.assertIsNone(body["validated_action"])
        self.assertIn("What should I create", body["speech"])

    def test_incomplete_ticket_create_asks_for_owner(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 28,
                "product_id": "linear-demo",
                "message": "Create a ticket.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertIsNone(body["proposed_action"])
        self.assertIsNone(body["validated_action"])
        self.assertIn("Who should own this ticket", body["speech"])

    def test_broad_workspace_request_is_clarified_without_action(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 25,
                "product_id": "linear-demo",
                "message": "Show me all company projects.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertIsNone(body["proposed_action"])
        self.assertIsNone(body["validated_action"])
        self.assertIn("I can only show work inside Product Engineering Workspace", body["speech"])

    def test_voice_interruption_question_uses_reasoning_policy(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 27,
                "product_id": "linear-demo",
                "message": "How do you stop speaking when I interrupt you?",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertIsNone(body["validated_action"])
        self.assertEqual(body["intent_trace"]["relevant_feature"], "Voice")
        self.assertIn("stop the current response", body["speech"])

    def test_llm_reasoner_handles_paraphrased_planning_request(self) -> None:
        agent = DemoAgent()
        fake_reasoner = FakeGeminiReasoner(
            AgentReasoningResult(
                speech="That maps to weekly planning. I'll open Cycles for this workspace.",
                proposed_action=ReasonedAction(type="OPEN_CYCLES", payload={}),
                clarification_question=None,
                intent_trace=IntentTrace(
                    goal="Understand weekly planning",
                    current_intent="Map paraphrase to product workflow",
                    relevant_feature="Cycles",
                    reason="The visitor asked how the team plans work week by week.",
                    confidence=0.91,
                    status="active",
                ),
            )
        )
        agent.llm_reasoner = fake_reasoner

        response = agent.handle_turn(
            TurnRequest(
                session_id="session_llm",
                turn_id=1,
                product_id="linear-demo",
                message="How do we pick the next batch of work?",
                input_mode="text",
                current_page="dashboard",
            ),
            DEMO_ADMIN,
            ProductDataStore().load(),  # the turn endpoint passes the caller's records
        )

        self.assertEqual(response.status, "completed")
        self.assertEqual(response.validated_action.type, "OPEN_CYCLES")
        self.assertEqual(response.intent_trace.relevant_feature, "Cycles")
        self.assertIn("weekly planning", response.speech)
        self.assertEqual(fake_reasoner.context_messages, ["How do we pick the next batch of work?"])

    def test_strong_weekly_planning_signal_does_not_get_overridden_by_llm(self) -> None:
        agent = DemoAgent()
        fake_reasoner = FakeGeminiReasoner(
            AgentReasoningResult(
                speech="I'll open Teams.",
                proposed_action=ReasonedAction(type="OPEN_TEAMS", payload={}),
                clarification_question=None,
                intent_trace=IntentTrace(
                    goal="Team workflow",
                    current_intent="Understand team work",
                    relevant_feature="Teams",
                    reason="The visitor mentioned their team.",
                    confidence=0.86,
                    status="active",
                ),
            )
        )
        agent.llm_reasoner = fake_reasoner

        response = agent.handle_turn(
            TurnRequest(
                session_id="session_llm_no_override",
                turn_id=1,
                product_id="linear-demo",
                message="How does my team plan work week by week?",
                input_mode="text",
                current_page="dashboard",
            ),
            DEMO_ADMIN,
            ProductDataStore().load(),  # the turn endpoint passes the caller's records
        )

        self.assertEqual(response.status, "completed")
        self.assertEqual(response.validated_action.type, "OPEN_CYCLES")
        self.assertEqual(response.intent_trace.relevant_feature, "Cycles")
        self.assertEqual(fake_reasoner.call_count, 0)

    def test_llm_reasoner_output_is_still_blocked_by_validator(self) -> None:
        agent = DemoAgent()
        fake_reasoner = FakeGeminiReasoner(
            AgentReasoningResult(
                speech="I'll open Salesforce.",
                proposed_action=ReasonedAction(type="OPEN_SALESFORCE", payload={}),
                clarification_question=None,
                intent_trace=IntentTrace(
                    goal="External CRM",
                    current_intent="Open external app",
                    relevant_feature="Integrations",
                    reason="The visitor asked for an app outside Pixel.",
                    confidence=0.9,
                    status="active",
                ),
            )
        )
        agent.llm_reasoner = fake_reasoner

        response = agent.handle_turn(
            TurnRequest(
                session_id="session_llm_guard",
                turn_id=1,
                product_id="linear-demo",
                message="Can you pull up our CRM pipeline?",
                input_mode="text",
                current_page="dashboard",
            ),
            DEMO_ADMIN,
            ProductDataStore().load(),  # the turn endpoint passes the caller's records
        )

        self.assertEqual(response.status, "denied")
        self.assertEqual(response.proposed_action.type, "OPEN_SALESFORCE")
        self.assertIsNone(response.validated_action)
        self.assertIn("I can only demonstrate Pixel workflows", response.speech)

    def test_external_crm_request_is_blocked_before_llm(self) -> None:
        agent = DemoAgent()
        fake_reasoner = FakeGeminiReasoner(
            AgentReasoningResult(
                speech="Would you like to check integrations instead?",
                proposed_action=None,
                clarification_question=None,
                intent_trace=IntentTrace(
                    goal="External CRM",
                    current_intent="Ask about CRM pipeline",
                    relevant_feature="Integrations",
                    reason="The visitor asked about an external CRM.",
                    confidence=0.83,
                    status="active",
                ),
            )
        )
        agent.llm_reasoner = fake_reasoner

        response = agent.handle_turn(
            TurnRequest(
                session_id="session_llm_hard_guard",
                turn_id=1,
                product_id="linear-demo",
                message="Can you pull up our Salesforce pipeline?",
                input_mode="text",
                current_page="dashboard",
            ),
            DEMO_ADMIN,
            ProductDataStore().load(),  # the turn endpoint passes the caller's records
        )

        self.assertEqual(response.status, "denied")
        self.assertEqual(response.proposed_action.type, "OPEN_SALESFORCE")
        self.assertIsNone(response.validated_action)
        self.assertEqual(fake_reasoner.call_count, 0)

    def test_external_email_request_is_blocked_before_llm(self) -> None:
        agent = DemoAgent()
        fake_reasoner = FakeGeminiReasoner(
            AgentReasoningResult(
                speech="I can explain issue notifications.",
                proposed_action=None,
                clarification_question=None,
                intent_trace=IntentTrace(
                    goal="External email",
                    current_intent="Open external email",
                    relevant_feature="Integrations",
                    reason="The visitor asked for email access.",
                    confidence=0.82,
                    status="active",
                ),
            )
        )
        agent.llm_reasoner = fake_reasoner

        response = agent.handle_turn(
            TurnRequest(
                session_id="session_llm_email_guard",
                turn_id=1,
                product_id="linear-demo",
                message="Can you check my Gmail inbox?",
                input_mode="text",
                current_page="dashboard",
            ),
            DEMO_ADMIN,
            ProductDataStore().load(),  # the turn endpoint passes the caller's records
        )

        self.assertEqual(response.status, "denied")
        self.assertEqual(response.proposed_action.type, "OPEN_GMAIL")
        self.assertIsNone(response.validated_action)
        self.assertEqual(fake_reasoner.call_count, 0)

    def test_destructive_request_is_blocked_before_llm(self) -> None:
        agent = DemoAgent()
        fake_reasoner = FakeGeminiReasoner(
            AgentReasoningResult(
                speech="I can clean up the board.",
                proposed_action=None,
                clarification_question=None,
                intent_trace=IntentTrace(
                    goal="Delete work",
                    current_intent="Delete issues",
                    relevant_feature="Issues",
                    reason="The visitor asked to delete work.",
                    confidence=0.88,
                    status="active",
                ),
            )
        )
        agent.llm_reasoner = fake_reasoner

        response = agent.handle_turn(
            TurnRequest(
                session_id="session_llm_delete_guard",
                turn_id=1,
                product_id="linear-demo",
                message="Can you delete every ticket in this project?",
                input_mode="text",
                current_page="issues",
            ),
            DEMO_ADMIN,
            ProductDataStore().load(),  # the turn endpoint passes the caller's records
        )

        self.assertEqual(response.status, "denied")
        self.assertEqual(response.proposed_action.type, "DELETE_ISSUES")
        self.assertIsNone(response.validated_action)
        self.assertEqual(fake_reasoner.call_count, 0)

    def test_correction_prefers_positive_feature_over_negated_feature(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 13,
                "product_id": "linear-demo",
                "message": "No, not cycles, show issues instead.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "OPEN_ISSUES")
        self.assertEqual(body["intent_trace"]["relevant_feature"], "Issues")

    def test_current_issue_context_supports_this_issue_followup(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 14,
                "product_id": "linear-demo",
                "message": "How do I assign this issue?",
                "input_mode": "text",
                "current_page": "issue_detail",
                "selected_issue_id": "LIN-137",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "HIGHLIGHT_ASSIGNMENT_CONTROL")
        self.assertEqual(body["validated_action"]["payload"]["issue_id"], "LIN-137")

    def test_unknown_person_ticket_lookup_falls_back_to_issues(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 5,
                "product_id": "linear-demo",
                "message": "Open the ticket created for Alex.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "OPEN_ISSUES")
        self.assertIn("could not find a ticket for Alex", body["speech"])
        self.assertEqual(body["retrieved_context"][0]["source"], "issues.md")

    def test_new_ticket_request_creates_demo_issue(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 15,
                "product_id": "linear-demo",
                "message": "Open a fresh ticket for Maya.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["proposed_action"]["type"], "CREATE_DEMO_ISSUE")
        self.assertEqual(body["validated_action"]["type"], "CREATE_DEMO_ISSUE")
        self.assertEqual(body["validated_action"]["payload"]["assignee"], "Maya Chen")
        self.assertEqual(body["validated_action"]["payload"]["status"], "Todo")
        self.assertIn("I created", body["speech"])

    def test_ticket_creation_for_unknown_person_opens_team_directory(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 24,
                "product_id": "linear-demo",
                "message": "Create a ticket for Lucifer.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "HIGHLIGHT_ADD_MEMBER_BUTTON")
        self.assertEqual(body["validated_action"]["payload"]["name"], "Lucifer")
        self.assertIn("Lucifer is not in the team directory yet", body["speech"])
        self.assertNotIn("I created", body["speech"])

    def test_selected_issue_follow_up_updates_assignee(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 25,
                "product_id": "linear-demo",
                "message": "Assign it to Noah",
                "input_mode": "text",
                "selected_issue_id": "LIN-142",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "UPDATE_DEMO_ISSUE")
        self.assertEqual(body["validated_action"]["payload"]["issue_id"], "LIN-142")
        self.assertEqual(body["validated_action"]["payload"]["assignee"], "Noah Patel")
        self.assertIn("assignee is now Noah Patel", body["speech"])

    def test_selected_issue_follow_up_updates_priority(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 26,
                "product_id": "linear-demo",
                "message": "Make it high priority",
                "input_mode": "text",
                "selected_issue_id": "LIN-137",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "UPDATE_DEMO_ISSUE")
        self.assertEqual(body["validated_action"]["payload"]["priority"], "High")
        self.assertIn("priority is now High", body["speech"])

    def test_new_ticket_workflow_question_opens_issues(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 16,
                "product_id": "linear-demo",
                "message": "Show me how to create a ticket.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "HIGHLIGHT_CREATE_TICKET_BUTTON")
        self.assertIn("highlight Create ticket", body["speech"])

    def test_best_way_to_create_ticket_opens_issue_workflow(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 22,
                "product_id": "linear-demo",
                "message": "So what is the best way to create a ticket?",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "HIGHLIGHT_CREATE_TICKET_BUTTON")
        self.assertIn("highlight Create ticket", body["speech"])

    def test_assignment_question_highlights_assignment_control(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 6,
                "product_id": "linear-demo",
                "message": "How do I assign Maya's ticket to one developer?",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "HIGHLIGHT_ASSIGNMENT_CONTROL")
        self.assertEqual(body["validated_action"]["payload"]["issue_id"], "LIN-142")
        self.assertEqual(body["intent_trace"]["current_intent"], "Highlight assignment control")
        self.assertIsNone(body["intent_trace"]["role"])
        self.assertEqual(body["retrieved_context"][0]["source"], "issues.md")
        self.assertIn("Assignment is handled from the issue detail panel.", body["speech"])

    def test_github_question_retrieves_integrations_doc(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 7,
                "product_id": "linear-demo",
                "message": "How does the GitHub integration work?",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "HIGHLIGHT_GITHUB_CARD")
        self.assertEqual(body["retrieved_context"][0]["source"], "integrations.md")

    def test_system_architecture_request_opens_architecture_view(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 29,
                "product_id": "linear-demo",
                "message": "Open the system architecture.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "OPEN_SYSTEM_ARCHITECTURE")
        self.assertEqual(body["intent_trace"]["relevant_feature"], "Architecture")
        self.assertIn("system architecture view", body["speech"])

    def test_slack_question_highlights_slack_card(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 23,
                "product_id": "linear-demo",
                "message": "What can Pixel do with Slack?",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "HIGHLIGHT_SLACK_CARD")
        self.assertIn("Slack lets teams create issues", body["speech"])
        self.assertEqual(body["retrieved_context"][0]["source"], "integrations.md")

    def test_github_setup_opens_setup_flow(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 21,
                "product_id": "linear-demo",
                "message": "Set up the GitHub integration.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "OPEN_GITHUB_SETUP")
        self.assertEqual(body["intent_trace"]["current_intent"], "Configure integration")
        self.assertIn("GitHub setup flow", body["speech"])

    def test_team_capacity_question_retrieves_teams_doc(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 8,
                "product_id": "linear-demo",
                "message": "Show me team capacity and workload.",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "OPEN_TEAMS")
        self.assertEqual(body["retrieved_context"][0]["source"], "teams.md")

    def test_team_count_question_answers_count_and_opens_teams(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 18,
                "product_id": "linear-demo",
                "message": "How many team members are there?",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["validated_action"]["type"], "OPEN_TEAMS")
        self.assertIn("There are 2 team members", body["speech"])

    def test_capability_question_answers_without_navigation(self) -> None:
        response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 19,
                "product_id": "linear-demo",
                "message": "Are you capable of doing?",
                "input_mode": "text",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertIsNone(body["validated_action"])
        self.assertIn("I can guide this Pixel demo", body["speech"])

    def test_completed_turn_is_not_active_after_response(self) -> None:
        self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 3,
                "product_id": "linear-demo",
                "message": "Show bug tracking.",
                "input_mode": "text",
            },
        )

        response = self.client.post(
            "/api/turn/3/cancel",
            json={"session_id": "session_test"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "not_active")

    def test_cancel_active_turn(self) -> None:
        sessions = SessionManager()
        # The API client is signed in as demo-admin, so the session must belong to that user.
        sessions.ensure_session("session_test", "linear-demo", user_id="demo-admin", tenant_id="pixel-dev")
        self.assertTrue(sessions.activate_turn("session_test", 9))

        response = self.client.post(
            "/api/turn/9/cancel",
            json={"session_id": "session_test"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertFalse(sessions.is_active_turn("session_test", 9))

    def test_older_turn_cannot_replace_newer_turn(self) -> None:
        first_response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 11,
                "product_id": "linear-demo",
                "message": "Show sprint planning.",
                "input_mode": "text",
            },
        )
        stale_response = self.client.post(
            "/api/turn",
            json={
                "session_id": "session_test",
                "turn_id": 10,
                "product_id": "linear-demo",
                "message": "Show bug tracking.",
                "input_mode": "text",
            },
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.json()["status"], "completed")
        self.assertEqual(stale_response.status_code, 200)
        self.assertEqual(stale_response.json()["status"], "stale")
        self.assertEqual(stale_response.json()["intent_trace"]["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
