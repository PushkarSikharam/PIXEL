"""Milestone 2: reasoning token limits and persistent (not in-memory) call limits."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError

from app.definitions.access import authorize_product
from app.schemas import TurnRequest
from app.services.agent import DemoAgent
from app.services.agent_reasoner import AgentReasoner, estimate_tokens
from app.services.provider_policy import ReasoningTokenLimits
from app.services.retriever import RetrievedDocument
from app.services.product_data_store import ProductDataStore
from app.workspace_config import get_workspace_scope
from test_usage_ledger import ORG_ADMIN, LedgerFixture, SECRET_MESSAGE

DEFAULT_LIMITS = ReasoningTokenLimits(max_input_tokens=4_000, max_output_tokens=512)


class ReasoningLimitsTest(LedgerFixture):
    def setUp(self):
        super().setUp()
        os.environ.update({"LLM_ENABLED": "true", "LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "fake-key"})
        self.calls: list[dict] = []
        self.reasoner = AgentReasoner(transport=self.transport)

    def transport(self, api_key, payload, timeout_ms):
        self.calls.append(payload)
        raise TimeoutError()

    def context(self, **overrides):
        return replace(self.reasoning_context(), **overrides)

    @staticmethod
    def prompt_of(payload: dict) -> str:
        return payload["contents"][0]["parts"][0]["text"]

    def test_output_tokens_are_capped(self):
        payload = self.reasoner._payload(self.context(), DEFAULT_LIMITS)
        self.assertEqual(payload["generationConfig"]["maxOutputTokens"], 512)

    def test_oversized_optional_context_is_trimmed_and_request_kept(self):
        huge = RetrievedDocument(title="Guide", source="guide.md", snippet="x" * 30_000, score=1)
        payload = self.reasoner._payload(self.context(retrieved_docs=[huge]), DEFAULT_LIMITS)
        prompt = self.prompt_of(payload)
        self.assertLessEqual(estimate_tokens(json.dumps(payload)), 4_000)
        self.assertNotIn("x" * 100, prompt)
        self.assertIn(f"Visitor message: {SECRET_MESSAGE}", prompt)
        self.assertIn("Stay inside the active workspace", prompt)
        self.assertIn("Allowed actions:", prompt)

    def test_older_context_is_trimmed_before_product_docs(self):
        context = self.context()
        sections = self.reasoner._prompt_sections(context)
        without_issues = "\n".join(s.text for s in sections if not s.text.startswith("Visible issues"))
        limit = estimate_tokens(json.dumps(self.reasoner._request_body(without_issues, 512)))

        payload = self.reasoner._payload(context, replace(DEFAULT_LIMITS, max_input_tokens=limit))
        prompt = self.prompt_of(payload)
        self.assertNotIn("Visible issues", prompt)
        self.assertIn("Visible team members", prompt)
        self.assertIn("Product docs", prompt)

    def test_request_that_cannot_fit_is_never_sent_or_reserved(self):
        os.environ["LLM_MAX_INPUT_TOKENS"] = "300"
        self.assertIsNone(self.reasoner.reason(self.context()))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.rows(), [])

    def test_reservation_covers_estimated_input_plus_output_cap(self):
        self.reasoner.reason(self.context())
        sent = self.calls[0]
        self.assertEqual(self.rows()[0]["reserved_units"], estimate_tokens(json.dumps(sent)) + 512)

    def test_visitor_messages_are_length_limited(self):
        base = {"session_id": "s", "turn_id": 1, "product_id": "linear-demo"}
        TurnRequest(**base, message="x" * 2000)
        with self.assertRaises(ValidationError):
            TurnRequest(**base, message="x" * 2001)


class PersistentSessionLimitTest(LedgerFixture):
    """The old in-memory per-session counter is gone; the ledger limit survives restarts."""

    def test_session_limit_applies_across_agent_restarts(self):
        os.environ.update({
            "LLM_ENABLED": "true", "LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "fake-key",
            "PIXEL_BUDGET_REASONING_SESSION_ATTEMPTS": "1",
        })
        calls: list[dict] = []

        def transport(api_key, payload, timeout_ms):
            calls.append(payload)
            raise TimeoutError()

        request = TurnRequest(session_id="s1", turn_id=1, product_id="linear-demo",
                              message="how should my team plan work")
        access = authorize_product(ORG_ADMIN, "linear-demo")
        data = ProductDataStore().load()
        scope = get_workspace_scope("workspace-product-eng", data)
        for _ in range(2):
            agent = DemoAgent()  # a fresh process-level agent each time
            self.assertFalse(hasattr(agent, "llm_call_counts"))
            agent.llm_reasoner = AgentReasoner(transport=transport)
            agent._reason_with_llm(request, request.message, scope, "demo-product-eng", access,
                                   "linear_simplified", data)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.statuses(), ["timeout", "blocked"])


if __name__ == "__main__":
    unittest.main()
