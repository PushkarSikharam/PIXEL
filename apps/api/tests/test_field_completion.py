"""Milestone 3.2, Slice 5c: required-field completion for creating a record (plan, section 7).

A create asks for each required field the pinned definition neither received nor defaults, one at
a time, in definition order, and proposes only once the completed create validates. Nothing is
guessed; unknown and inaccessible answers read the same; a cancellation, a new request or two
unusable answers set the unfinished create aside.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from app import main  # noqa: E402
from app.definitions.contract import FieldSpec  # noqa: E402
from app.engine.field_completion import read_answer  # noqa: E402
from app.engine.lookup import RecordView  # noqa: E402
from app.engine.snapshot import TurnSnapshot  # noqa: E402
from test_turn_execution import NewEngineFixture, SCOPE  # noqa: E402

TENANT, PRODUCT = "pixel-dev", "linear-demo"
CREATE = "Create a ticket for Noah about login errors"


def snapshot(*projects: tuple[str, str]) -> TurnSnapshot:
    return TurnSnapshot(
        records={"project": tuple(RecordView("project", pid, title, {}) for pid, title in projects)},
        scope_label="scope", definition_checksum="x", taken_at=1.0,
    )


class ReadAnswerTest(unittest.TestCase):
    def test_each_type_reads_only_a_clear_answer(self):
        empty = snapshot()
        enum = FieldSpec(type="enum", values=["Low", "Medium", "High"])
        self.assertEqual(read_answer(enum, "make it high", empty), "High")
        self.assertIsNone(read_answer(enum, "high or low", empty), "two values are no answer")
        self.assertIsNone(read_answer(enum, "urgent", empty))
        self.assertEqual(read_answer(FieldSpec(type="integer"), "42", empty), 42)
        self.assertIsNone(read_answer(FieldSpec(type="integer"), "about forty", empty))
        self.assertIs(read_answer(FieldSpec(type="boolean"), "yes please", empty), True)
        self.assertIs(read_answer(FieldSpec(type="boolean"), "no", empty), False)
        self.assertIsNone(read_answer(FieldSpec(type="boolean"), "maybe", empty))
        self.assertEqual(read_answer(FieldSpec(type="date"), "on 2026-10-01", empty), "2026-10-01")
        self.assertIsNone(read_answer(FieldSpec(type="date"), "next friday", empty))
        self.assertEqual(read_answer(FieldSpec(type="text"), "  'Fix login redirect.' ", empty),
                         "Fix login redirect")

    def test_a_reference_is_one_visible_record_or_nothing(self):
        visible = snapshot(("PRJ-1", "Issue Triage Workflow"), ("PRJ-2", "Issue Intake"))
        project = FieldSpec(type="ref", target="project")
        self.assertEqual(read_answer(project, "the issue triage workflow project", visible), "PRJ-1")
        self.assertEqual(read_answer(project, "prj-2", visible), "PRJ-2")
        self.assertIsNone(read_answer(project, "issue", visible), "ambiguous reads as no answer")
        self.assertIsNone(read_answer(project, "Platform Reliability", visible), "unseen reads as unknown")


class CreateCompletionTest(NewEngineFixture):
    def visible_project_names(self, scope: str = SCOPE) -> list[str]:
        return [project["name"] for project in main.product_data.load(frozenset({scope}))["projects"]]

    def test_the_current_version_defaults_priority_and_status_and_asks_for_the_project(self):
        asked = self.say(CREATE)
        self.assertIsNone(asked["execution"], "nothing is dispatched while the project is missing")
        self.assertIsNone(asked["validated_action"])
        self.assertTrue(asked["speech"].startswith("Which project should the new ticket have:"), asked["speech"])
        for name in self.visible_project_names():
            self.assertIn(name, asked["speech"])

        proposed = self.say("Issue Triage Workflow")
        self.assertEqual(proposed["validated_action"]["type"], "CREATE_DEMO_ISSUE")
        self.assertIsNotNone(proposed["execution"])
        fields = proposed["validated_action"]["payload"]
        self.assertEqual((fields["priority"], fields["status"]), ("Medium", "Todo"), "declared defaults only")
        receipt = self.write(proposed)
        self.assertEqual(receipt.status_code, 200, receipt.text)
        record = receipt.json()["record"]
        self.assertEqual((record["assignee"], record["project"]), ("Noah Patel", "Issue Triage Workflow"))

    def test_every_workspace_can_create_in_its_own_projects(self):
        """The regression v3 shipped: a defaulted project outside the workspace refused every create."""
        say = lambda message: self.client.post("/api/turn", headers=self.headers, json={  # noqa: E731
            "session_id": "platform", "turn_id": next(turns), "product_id": PRODUCT, "message": message,
            "workspace_scope_id": "workspace-platform",
        }).json()
        turns = iter(range(1, 10))
        asked = say("Create a ticket for Avery about flaky deploys")
        self.assertTrue(asked["speech"].startswith("Which project should the new ticket have:"), asked["speech"])
        platform_project = self.visible_project_names("workspace-platform")[-1]
        proposed = say(platform_project)
        self.assertEqual(proposed["validated_action"]["type"], "CREATE_DEMO_ISSUE")
        self.assertIsNotNone(proposed["execution"])

    def test_v2_asks_for_every_undefaulted_field_in_definition_order(self):
        main.agent.directory.move_product_version(TENANT, PRODUCT, 2)
        self.assertIn("What priority should the new ticket have: Low, Medium or High?", self.say(CREATE)["speech"])
        self.assertIn("What status should the new ticket have:", self.say("high")["speech"])
        self.assertIn("Which project should the new ticket have", self.say("todo")["speech"])
        proposed = self.say("Issue Triage Workflow")
        fields = proposed["validated_action"]["payload"]
        self.assertEqual((fields["priority"], fields["status"]), ("High", "Todo"))
        self.assertIsNotNone(proposed["execution"])

    def test_a_project_from_another_workspace_is_never_accepted_or_confirmed(self):
        for index, elsewhere in enumerate(self.visible_project_names("workspace-platform")):
            with self.subTest(project=elsewhere):
                session = f"elsewhere-{index}"
                self.say(CREATE, session=session)
                answer = self.say(elsewhere, session=session)
                self.assertIsNone(answer["execution"])
                self.assertNotEqual((answer["validated_action"] or {}).get("type"), "CREATE_DEMO_ISSUE")
                self.assertNotIn(elsewhere, answer["speech"])

    def test_a_cancellation_sets_the_unfinished_create_aside(self):
        main.agent.directory.move_product_version(TENANT, PRODUCT, 2)
        self.say(CREATE)
        self.assertEqual(self.say("never mind")["intent_trace"]["current_intent"], "cancelled")
        after = self.say("high")
        self.assertIsNone(after["execution"], "an answer after cancelling creates nothing")

    def test_a_new_request_replaces_the_unfinished_create(self):
        main.agent.directory.move_product_version(TENANT, PRODUCT, 2)
        self.say(CREATE)
        navigation = self.say("Show me the cycles")
        self.assertEqual(navigation["validated_action"]["type"], "OPEN_CYCLES")
        after = self.say("high")
        self.assertIsNone(after["execution"])

    def test_two_unusable_answers_set_the_create_aside(self):
        main.agent.directory.move_product_version(TENANT, PRODUCT, 2)
        self.say(CREATE)
        self.assertIn("What priority", self.say("the blue one")["speech"])
        self.assertEqual(self.say("the other blue one")["intent_trace"]["current_intent"], "cancelled")


if __name__ == "__main__":
    unittest.main()
