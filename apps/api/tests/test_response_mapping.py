"""Milestone 3.2, Slice 5c: every public response field and every engine stage (plan, section 9).

Each `TurnResponse` field has one named source, and every engine stage declares its status, speech,
action and execution behaviour. A new stage fails here until it is declared.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.engine.conversation_engine import ConversationEngine, TurnStage  # noqa: E402
from app.schemas import TurnResponse  # noqa: E402
from app.services.turn_execution import EXECUTABLE_STAGES, STAGE_STATUS  # noqa: E402
from test_turn_execution import NewEngineFixture  # noqa: E402

MUTATION = "assign it to Noah"


class StageContractTest(unittest.TestCase):
    def test_every_stage_declares_its_status(self):
        self.assertEqual(set(STAGE_STATUS), set(TurnStage), "declare the public status of every new stage")
        self.assertEqual({stage for stage, status in STAGE_STATUS.items() if status == "denied"},
                         {TurnStage.REFUSED})

    def test_every_public_field_is_mapped(self):
        self.assertEqual(set(TurnResponse.model_fields), {
            "session_id", "turn_id", "status", "speech", "proposed_action", "validated_action",
            "intent_trace", "signals", "retrieved_context", "session_summary", "execution",
        }, "a new public field needs a named source here and in turn_execution._response")


class StageMappingTest(NewEngineFixture):
    """Each stage, forced on the same mutation turn, answers exactly as declared."""

    def turn_as(self, stage: TurnStage) -> tuple[dict, object]:
        real_turn = ConversationEngine.turn
        captured = {}

        def forced(engine, *args, **kwargs):
            captured["turn"] = replace(real_turn(engine, *args, **kwargs), stage=stage)
            return captured["turn"]

        self.say("Open Maya's ticket")
        with patch.object(ConversationEngine, "turn", forced):
            body = self.say(MUTATION)
        return body, captured["turn"]

    def test_each_stage(self):
        for stage in TurnStage:
            with self.subTest(stage=stage):
                self.setUp()
                body, turn = self.turn_as(stage)
                self.assertEqual(body["status"], STAGE_STATUS[stage])
                self.assertEqual(body["speech"], turn.reply.speech, "speech is the platform composition")
                self.assertEqual(body["intent_trace"]["current_intent"], str(stage))
                executable = stage in EXECUTABLE_STAGES
                self.assertEqual(body["execution"] is not None, executable,
                                 "only a stage that may execute now carries a key")
                self.assertEqual(body["validated_action"] is not None, executable)
                self.assertEqual(body["proposed_action"]["type"], "UPDATE_DEMO_ISSUE",
                                 "the proposal is reported whatever the stage")
                self.doCleanups()

    def test_the_remaining_fields_come_from_the_request_and_the_engine(self):
        self.say("Open Maya's ticket")
        real_turn = ConversationEngine.turn
        captured = {}

        def spy(engine, *args, **kwargs):
            captured["turn"] = real_turn(engine, *args, **kwargs)
            return captured["turn"]

        with patch.object(ConversationEngine, "turn", spy):
            body = self.say("How do cycles work?")
        turn = captured["turn"]
        self.assertEqual((body["session_id"], body["turn_id"]), ("new-engine", self.turn_id))
        self.assertEqual([(s["type"], s["value"]) for s in body["signals"]],
                         [(s.type, s.value) for s in turn.signals])
        self.assertEqual([c["source"] for c in body["retrieved_context"]], [p.source for p in turn.passages])
        self.assertEqual(body["session_summary"]["interests"], list(turn.summary.interests))
        self.assertEqual(body["session_summary"]["last_feature"], turn.summary.last_feature)


if __name__ == "__main__":
    unittest.main()
