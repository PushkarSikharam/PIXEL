"""Milestone 3.2, Slice 5c: conversation the browser used to answer by itself (plan, section 2.3).

The browser now sends every message to `/api/turn`. Each case below was a browser-local branch
before 5c; the backend must answer it, through the same validator, without guessing.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_engine_cutover import EngineCutoverFixture  # noqa: E402

PLATFORM = "workspace-platform"


class BackendConversationTest(EngineCutoverFixture):
    def say(self, message: str, *, session: str = "talk", turn_id: int = 1, scope: str | None = None,
            page: str = "dashboard") -> dict:
        body = {"session_id": session, "turn_id": turn_id, "product_id": "linear-demo", "message": message,
                "input_mode": "text", "current_page": page,
                "workspace_scope_id": scope or "workspace-product-eng"}
        response = self.client.post("/api/turn", headers=self.headers, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def action(self, body: dict) -> tuple[str | None, dict]:
        action = body["validated_action"] or {}
        return action.get("type"), action.get("payload") or {}

    def test_identity_and_capabilities_name_the_guide_and_the_workspace(self):
        self.assertIn("I'm Edith, Pixel's live demo guide.", self.say("Hii there who are u")["speech"])
        speech = self.say("What are you capable of doing?", session="caps")["speech"]
        self.assertIn("I can guide this Pixel demo through Product Engineering Workspace", speech)

    def test_a_greeting_remembers_only_this_sessions_introduction(self):
        self.assertIn("Nice to meet you, Pushkar.", self.say("HI there i am Pushkar!")["speech"])
        self.assertEqual(self.say("Hi", turn_id=2)["speech"],
                         "Hi Pushkar. What would you like to explore next in Pixel?")
        self.assertEqual(self.say("Hi", session="someone-else")["speech"],
                         "Hi there. What would you like to explore first in Pixel?")

    def test_a_phrase_that_is_not_an_introduction_names_nobody(self):
        self.assertNotIn("Nice to meet you", self.say("I'm not sure")["speech"])

    def test_the_guided_path_opens_the_dashboard(self):
        body = self.say("run the evaluator demo")
        self.assertIn("Here is a clean guided path", body["speech"])
        self.assertEqual(self.action(body)[0], "OPEN_DASHBOARD")

    def test_the_next_step_depends_on_the_current_view(self):
        self.assertIn("sprint planning", self.say("what next")["speech"])
        self.assertIn("assign it to Noah", self.say("what next", session="n2", page="issue_detail")["speech"])

    def test_adding_a_member_carries_the_name(self):
        body = self.say("can you add a new member Lucife")
        self.assertEqual(self.action(body), ("HIGHLIGHT_ADD_MEMBER_BUTTON", {"name": "Lucife"}))
        unnamed = self.say("add a new teammate", session="m2")
        self.assertEqual(self.action(unnamed), ("HIGHLIGHT_ADD_MEMBER_BUTTON", {}))
        self.assertEqual(unnamed["speech"], "Who should I add to the team directory?")

    def test_a_ticket_without_an_owner_asks_and_never_guesses_one(self):
        for index, message in enumerate(("create a ticket", "open a new ticket")):
            body = self.say(message, session=f"owner-{index}")
            self.assertIn("Who should own this ticket?", body["speech"])
            self.assertEqual(self.action(body)[0], "HIGHLIGHT_CREATE_TICKET_BUTTON")
            self.assertIsNone(body["execution"], "nothing is dispatched for an unowned ticket")

    def test_an_unknown_assignee_in_a_mixed_request_must_be_added_first(self):
        body = self.say("open a ticket for Maya and assign to Jen")
        self.assertEqual(self.action(body), ("HIGHLIGHT_ADD_MEMBER_BUTTON", {"name": "Jen"}))
        self.assertIn("Jen is not in the team directory yet", body["speech"])

    def test_a_person_in_another_workspace_reads_exactly_like_an_unknown_one(self):
        """Security (5c plan, section 3.3): the previous engine sees only the selected workspace, so
        it never confirms that someone exists elsewhere, even in rollback."""
        other_workspace = self.say("open Maya's ticket", scope=PLATFORM, session="maya")
        nobody = self.say("open Zed's ticket", scope=PLATFORM, session="zed")
        self.assertNotIn("Maya Chen", other_workspace["speech"])
        self.assertEqual(other_workspace["speech"].replace("Maya", "Zed"), nobody["speech"])
        self.assertNotEqual(self.action(other_workspace)[0], "OPEN_DEMO_ISSUE")
        created = self.say("create a ticket for Avery", session="avery")
        self.assertNotIn("Avery Brooks", created["speech"])
        self.assertIsNone(created["execution"])

    def test_counts_and_person_follow_ups_stay_in_the_workspace(self):
        projects = self.say("how many projects")
        self.assertIn("Product Engineering Workspace has 2 visible projects", projects["speech"])
        self.assertEqual(self.action(projects)[0], "OPEN_PROJECTS")
        self.say("all tickets for Maya", session="follow")
        follow = self.say("what about Noah", session="follow", turn_id=2)
        self.assertEqual(self.action(follow), ("FILTER_ISSUES_BY_ASSIGNEE", {"assignee": "Noah Patel"}))

    def test_what_changed_reads_only_the_committed_ledger(self):
        self.assertEqual(self.say("what did we just change?")["speech"],
                         "Nothing has changed in this conversation yet.")
        self.say("assign LIN-142 to Noah", turn_id=2)
        # Proposed and dispatched, not yet written: still nothing has changed.
        self.assertEqual(self.say("what changed", turn_id=3)["speech"],
                         "Nothing has changed in this conversation yet.")
        mutation = self.say("assign LIN-142 to Noah", turn_id=4)
        headers = {**self.headers, "X-Execution-Key": mutation["execution"]["key"], "X-Session-Id": "talk"}
        receipt = self.client.patch("/api/demo-data/issues/LIN-142",
                                    json={"changes": {"assignee": "Noah Patel"}}, headers=headers)
        self.assertEqual(receipt.status_code, 200, receipt.text)
        self.assertEqual(self.say("what did we just change?", turn_id=5)["speech"],
                         "The most recent change: updated LIN-142.")


class DefinitionAuthorityConversationTest(EngineCutoverFixture):
    """The same visitor requests under definition authority (5d plan revision 2, section 5).

    Each case was a definition-engine defect that blocked the 5c cutover: a visitor would have met
    it the moment `PIXEL_ENGINE_MODE=definition` was switched on.
    """

    def setUp(self):
        super().setUp()
        authority = patch.dict(os.environ, {"PIXEL_ENGINE_MODE": "definition"}, clear=False)
        authority.start()
        self.addCleanup(authority.stop)

    say = BackendConversationTest.say
    action = BackendConversationTest.action

    def test_starting_a_ticket_for_someone_drafts_a_new_one(self):
        for index, message in enumerate(("Start a ticket assigned to Noah", "open a new ticket for Noah",
                                         "draft a ticket for Noah")):
            body = self.say(message, session=f"start-{index}")
            self.assertNotEqual(self.action(body)[0], "OPEN_DEMO_ISSUE", message)
            self.assertNotIn("LIN-137", body["speech"], message)
            self.assertEqual(body["speech"], "What should the title of the new ticket be?", message)
            self.assertIsNone(body["execution"], message)

    def test_opening_someones_ticket_still_opens_it(self):
        body = self.say("open a ticket for Noah")
        self.assertEqual(self.action(body)[0], "OPEN_DEMO_ISSUE")
        self.assertEqual(body["speech"], "I'll open LIN-137.")

    def test_an_unknown_person_beside_a_known_one_is_added_first(self):
        for index, message in enumerate(("Open a ticket for Maya and assign to Jen",
                                         "start a ticket for Maya assigned to Jen",
                                         "create a ticket for Maya and assign it to Jen")):
            body = self.say(message, session=f"jen-{index}")
            self.assertEqual(self.action(body), ("HIGHLIGHT_ADD_MEMBER_BUTTON", {"name": "Jen"}), message)
            self.assertNotIn("LIN-142", body["speech"], message)
            self.assertIsNone(body["execution"], message)

    def test_what_we_just_changed_is_read_from_the_ledger_not_documentation(self):
        for index, question in enumerate(("What did we just change?", "what have we changed",
                                          "what just happened")):
            body = self.say(question, session=f"ledger-{index}")
            self.assertEqual(body["speech"], "Nothing has changed in this conversation yet.", question)
            self.assertNotIn("documentation", body["speech"])
        self.say("Open Maya's ticket", session="ledger")
        mutation = self.say("assign it to Noah", session="ledger", turn_id=2)
        headers = {**self.headers, "X-Execution-Key": mutation["execution"]["key"], "X-Session-Id": "ledger"}
        receipt = self.client.patch("/api/demo-data/issues/LIN-142",
                                    json={"changes": {"assignee": "Noah Patel"}}, headers=headers)
        self.assertEqual(receipt.status_code, 200, receipt.text)
        self.assertEqual(self.say("What did we just change?", session="ledger", turn_id=3)["speech"],
                         "The most recent change: updated LIN-142.")

    def test_a_profile_statement_is_acknowledged_not_failed(self):
        for index, message in enumerate((
            "I'm an engineering manager with a 12 person team moving from Jira",
            "we're a small team", "I am a product designer",
        )):
            body = self.say(message, session=f"profile-{index}")
            self.assertEqual(body["speech"], "Thanks, that helps. What would you like to explore first in Pixel?", message)
            self.assertIsNone(body["validated_action"], message)

    def test_a_request_after_a_self_description_is_still_served(self):
        body = self.say("I'm a manager, show me the projects")
        self.assertEqual(self.action(body)[0], "OPEN_PROJECTS")


if __name__ == "__main__":
    unittest.main()
