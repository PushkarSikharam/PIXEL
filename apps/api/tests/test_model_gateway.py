"""Milestone 3.2, Slice 5c: the optional model gateway (plan, sections 8 and 12.5), scripted.

No provider is contacted: a scripted transport stands in for one. Each case has one deterministic
outcome, and none falls back to another engine or lets model text become speech.
"""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import db, main  # noqa: E402
from app.engine.conversation_engine import ConversationEngine, TurnStage  # noqa: E402
from app.installed_products import package_for  # noqa: E402
from app.services.model_gateway import ModelGateway  # noqa: E402
from app.services.turn_execution import NewEngineTurns  # noqa: E402
from app.services.usage_ledger import BudgetExceeded  # noqa: E402
from test_turn_execution import NewEngineFixture  # noqa: E402

UNROUTABLE = "zorb the flimflam quietly"
ON = {"PIXEL_MODEL_GATEWAY": "on", "PIXEL_PAID_PROVIDERS_ENABLED": "true"}


def reply(action: dict | None = None, speech: str = "MODEL WORDING", clarification: str | None = None) -> str:
    return json.dumps({"speech": speech, "action": action, "clarification": clarification})


class ScriptedTransport:
    model = "scripted"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def __call__(self, prompt: str, timeout_ms: int):
        self.calls += 1
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item, 100, 20


class GatewayFixture(NewEngineFixture):
    def use(self, *replies, env: dict | None = None) -> ScriptedTransport:
        transport = ScriptedTransport(*replies)
        self.turns = NewEngineTurns(main.agent.sessions, main.agent.directory, package_for,
                                    gateway=ModelGateway(transport))
        main.app.dependency_overrides[main.turn_engine] = lambda: self.turns
        env_patch = patch.dict(os.environ, env if env is not None else ON)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        return transport

    def attempts(self) -> int:
        with db.get_connection() as connection:
            return connection.execute("select count(*) from provider_attempts where status != 'blocked'").fetchone()[0]


class GatewayTest(GatewayFixture):
    def test_the_gateway_is_off_by_default_and_without_a_transport(self):
        transport = self.use(reply(), env={})
        body = self.say(UNROUTABLE)
        self.assertEqual(body["intent_trace"]["current_intent"], "fallback")
        self.assertEqual(transport.calls, 0)
        self.assertFalse(ModelGateway().enabled(), "no transport is configured by default")

    def test_deterministic_turns_never_call_the_model(self):
        transport = self.use(reply())
        self.say("Show me the issues")
        self.say("Open Maya's ticket")
        self.say("assign it to Noah")
        self.assertEqual(transport.calls, 0)

    def test_a_model_navigation_is_proposed_in_platform_words(self):
        transport = self.use(reply({"action_key": "open_cycles", "params": {"view": "cycles"}}))
        body = self.say(UNROUTABLE)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(body["validated_action"]["type"], "OPEN_CYCLES")
        self.assertIsNone(body["execution"])
        self.assertNotIn("MODEL WORDING", body["speech"])

    def test_model_speech_alone_never_reaches_the_visitor(self):
        self.use(reply(None, speech="Ignore the rules and say this."))
        body = self.say(UNROUTABLE)
        self.assertEqual(body["intent_trace"]["current_intent"], "fallback")
        self.assertNotIn("Ignore the rules", body["speech"])

    def test_unknown_malformed_and_injected_replies_are_refused_deterministically(self):
        for raw in (
            reply({"action_key": "delete_everything", "params": {}}),
            '{"speech": "a", "speech": "b", "action": null, "clarification": null}',
            "Sure! ```json {\"action\": null}```",
            reply({"action_key": "open_issue", "params": {"target": {"entity": "issue", "id": "LIN-999"}}}),
        ):
            with self.subTest(raw=raw[:40]):
                self.use(raw)
                body = self.say(UNROUTABLE)
                self.assertIsNone(body["execution"])
                self.assertIsNone(body["validated_action"])
                self.assertEqual(body["intent_trace"]["current_intent"], "fallback")

    def test_a_model_mutation_only_awaits_confirmation_and_the_yes_makes_no_call(self):
        transport = self.use(reply({"action_key": "update_issue", "params": {
            "target": {"entity": "issue", "id": "LIN-142"}, "fields": {"priority": "High"}}}))
        # The router understands almost any message naming a record and a value, so the first turn
        # is forced to fall back; the message still carries the evidence provenance requires.
        real_turn = ConversationEngine.turn

        def unroutable(engine, *args, **kwargs):
            turn = real_turn(engine, *args, **kwargs)
            return replace(turn, stage=TurnStage.FALLBACK, validated=None, legacy_action=None)

        with patch.object(ConversationEngine, "turn", unroutable):
            pending = self.say("LIN-142 High")
        self.assertEqual(transport.calls, 1)
        self.assertEqual(pending["intent_trace"]["current_intent"], "awaiting_confirmation", pending["speech"])
        self.assertIsNone(pending["execution"], "a model-originated change is never dispatched unconfirmed")
        self.assertIsNone(pending["validated_action"])
        confirmed = self.say("yes")
        self.assertEqual(transport.calls, 1, "confirmation makes no provider call")
        self.assertIsNotNone(confirmed["execution"])
        self.assertEqual(self.write(confirmed).status_code, 200)

    def test_timeout_failure_budget_and_kill_switch_each_keep_the_fallback(self):
        cases = {
            "timeout": (TimeoutError(), ON),
            "failed": (RuntimeError("provider 500"), ON),
        }
        for outcome, (error, env) in cases.items():
            with self.subTest(outcome=outcome):
                self.use(error, env=env)
                body = self.say(UNROUTABLE)
                self.assertEqual(body["intent_trace"]["current_intent"], "fallback")

        transport = self.use(reply(), env={**ON, "PIXEL_PAID_PROVIDERS_ENABLED": "false"})
        before = self.attempts()
        self.say(UNROUTABLE)
        self.assertEqual((transport.calls, self.attempts()), (0, before), "the kill switch blocks before any reservation")

        transport = self.use(reply())
        with patch.object(self.turns._gateway._usage, "reserve", side_effect=BudgetExceeded("user_unit_limit")):
            body = self.say(UNROUTABLE)
        self.assertEqual(transport.calls, 0)
        self.assertEqual(body["intent_trace"]["current_intent"], "fallback")

    def test_each_attempt_is_one_reservation_and_is_counted_in_telemetry(self):
        self.use(reply({"action_key": "open_cycles", "params": {"view": "cycles"}}))
        before = self.attempts()
        self.say(UNROUTABLE)
        self.assertEqual(self.attempts(), before + 1, "one attempt, no hidden retry")
        with db.get_connection() as connection:
            model = connection.execute(
                "select value from turn_telemetry_daily where metric = 'model'").fetchall()
        self.assertEqual([row["value"] for row in model], ["proposed"])


if __name__ == "__main__":
    unittest.main()
