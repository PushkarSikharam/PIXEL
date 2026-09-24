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

from app.engine.conversation import Conversational, detect  # noqa: E402
from app.engine.normalizer import NormalizedMessage  # noqa: E402

PLATFORM = "workspace-platform"
PRODUCT_NAME = "Pixel Planning"


class BackendConversationTest(EngineCutoverFixture):
    def say(self, message: str, *, session: str = "talk", turn_id: int = 1, scope: str | None = None,
            page: str = "dashboard", selected: str | None = None) -> dict:
        body = {"session_id": session, "turn_id": turn_id, "product_id": "linear-demo", "message": message,
                "input_mode": "text", "current_page": page,
                "workspace_scope_id": scope or "workspace-product-eng"}
        if selected:
            body["selected_issue_id"] = selected
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

    def test_a_question_never_proposes_a_change(self):
        """Found in the 5c production shadow run: "capable of doing" proposed a status change."""
        for index, message in enumerate(("what is pixel capable of doing?", "how do I mark a ticket done?",
                                         "can I set a ticket to in progress?")):
            body = self.say(message, session=f"asked-{index}")
            self.assertIsNone(body["execution"], message)
            self.assertNotEqual((body["validated_action"] or {}).get("type"), "UPDATE_DEMO_ISSUE", message)

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

    # One message per kind of turn the platform answers itself. Every kind is listed, so a new
    # one cannot be added without saying here what a visitor types to reach it.
    PLATFORM_TURNS = {
        Conversational.GREETING: "hello",
        Conversational.GREETING_NAMED: "hello",
        Conversational.IDENTITY: "who are you",
        Conversational.CAPABILITIES: "what can you do",
        Conversational.LAST_CHANGE: "what changed",
        Conversational.THANKS: "thanks",
        Conversational.CLOSING: "that is all",
        Conversational.GUIDED_PATH: "run the guided demo",
        Conversational.NEXT_STEP: "what should i try next",
        Conversational.VOICE_INTERRUPTION: "can i interrupt the voice",
    }

    def test_every_turn_the_platform_answers_itself_is_answered(self):
        """A reply the platform builds must carry every value its wording needs. Reaching one
        without them raised, and the visitor got a server error instead of an answer."""
        self.assertEqual(set(self.PLATFORM_TURNS), set(Conversational), "every kind needs a message")
        for index, (kind, message) in enumerate(self.PLATFORM_TURNS.items()):
            with self.subTest(kind=kind):
                normalized = NormalizedMessage(original=message, full=message, focused=message)
                detected = detect(normalized, visitor_name="Priya" if "named" in kind else None)
                self.assertIsNotNone(detected, message)
                self.assertEqual(detected.kind, kind, message)
                body = self.say(message, session=f"platform-{index}")
                self.assertTrue(body["speech"].strip(), message)

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
            self.assertEqual(
                body["speech"],
                f"Thanks, that helps. What would you like to explore first in {PRODUCT_NAME}?",
                message,
            )
            self.assertIsNone(body["validated_action"], message)

    def test_conversation_never_takes_over_a_request(self):
        """Routing comes first: a conversational phrase inside a request does not answer it."""
        for index, message in enumerate((
            "help me create a ticket for Noah about login errors",
            "create a ticket for Noah about the next step for onboarding",
            "create a ticket for Noah about voice listening dropping words",
        )):
            body = self.say(message, session=f"request-{index}")
            self.assertTrue(body["speech"].startswith("Which project should the new ticket have"), message)
        refused = self.say("help me delete all issues", session="refusal")
        self.assertEqual(refused["speech"], "I can't delete or erase anything here.")
        self.assertEqual(self.say("delete the tickets, what can you do?", session="refusal-2")["status"], "denied")

    def test_a_conversational_question_beats_only_a_question_back(self):
        """A record word ("doing") may match an update; with nothing to act on, the question is answered."""
        capable = self.say("are you capable of doing")
        self.assertTrue(capable["speech"].startswith(f"Here's what I can do in {PRODUCT_NAME}:"),
                        capable["speech"])
        self.assertEqual(self.say("assign it", session="still-asks")["speech"], "Who should this record be assigned to?")

    def test_navigation_interrupts_an_unfinished_create_but_a_title_does_not(self):
        self.say("Start a ticket assigned to Noah", session="nav")
        moved = self.say("show me the cycles", session="nav", turn_id=2)
        self.assertEqual(self.action(moved)[0], "OPEN_CYCLES")
        self.say("Start a ticket assigned to Noah", session="title")
        titled = self.say("Investigate customer onboarding issue", session="title", turn_id=2)
        self.assertTrue(titled["speech"].startswith("Which project should the new ticket have"), titled["speech"])
        self.say("Create a ticket for Noah about login errors", session="project")
        home = self.say("open the dashboard", session="project", turn_id=2)
        self.assertEqual(self.action(home)[0], "OPEN_DASHBOARD")

    def test_platform_answers_never_name_another_workspaces_people(self):
        for index, message in enumerate(("run the evaluator demo", "what can you do", "what next")):
            speech = self.say(message, scope=PLATFORM, session=f"names-{index}")["speech"]
            for name in ("Maya", "Noah", "Salesforce"):
                self.assertNotIn(name, speech, message)

    def test_counts_filters_and_controls_are_named_from_the_definition(self):
        self.assertEqual(self.say("how many team members are there", scope=PLATFORM, session="c1")["speech"],
                         "Platform Workspace has 2 team members. I'll open Teams.")
        self.assertEqual(self.say("show all tickets for Avery", scope=PLATFORM, session="c2")["speech"],
                         "I found 1 ticket for Avery Brooks: LIN-131. I'll filter the available records.")
        self.assertEqual(self.say("how do i assign this issue", session="c3")["speech"],
                         "I'll open Issue Detail and highlight Assignee.")
        missing = self.say("create a ticket for Priya about login errors", session="c4")
        self.assertEqual(missing["speech"],
                         "I can't find Priya in this workspace. I'll open Teams and highlight Add member.")

    def test_a_later_greeting_uses_the_name_the_visitor_gave(self):
        self.assertEqual(self.say("HI there i am Pushkar!", session="named")["speech"],
                         f"Nice to meet you, Pushkar. What would you like to explore in {PRODUCT_NAME}?")
        self.assertEqual(self.say("Hi", session="named", turn_id=2)["speech"],
                         f"Hi Pushkar, good to see you again. What would you like to explore next in {PRODUCT_NAME}?")
        self.assertNotIn("Pushkar", self.say("Hi", session="someone-else")["speech"])

    def test_the_next_step_is_drawn_from_the_open_view(self):
        self.assertEqual(self.say("what should I try next", page="teams")["speech"],
                         "From Teams, you could show you where Add member is in Teams.")
        self.assertEqual(self.say("what next", session="home", page="dashboard")["speech"],
                         f"Ask what I can do in {PRODUCT_NAME} to see where to go next.")
        # A page the definition does not declare is never trusted.
        forged = self.say("what next", session="bogus", page="billing")
        self.assertEqual(forged["status"], "denied")
        self.assertIn("invalid_turn_page", forged["intent_trace"]["reason"])

    def test_turn_context_selected_record_must_belong_to_this_product_and_scope(self):
        valid = self.say("what next", session="selected", page="issue_detail", selected="LIN-142")
        self.assertNotEqual(valid["status"], "denied")
        forged = self.say("what next", session="forged", page="issue_detail", selected="CON-7")
        self.assertEqual(forged["status"], "denied")
        self.assertIn("invalid_selected_record", forged["intent_trace"]["reason"])
        hidden = self.say("what next", session="hidden-selected", page="issue_detail",
                          scope=PLATFORM, selected="LIN-142")
        self.assertEqual(hidden["status"], "denied")
        self.assertIn("invalid_selected_record", hidden["intent_trace"]["reason"])

    def test_adding_a_member_without_a_name_asks_for_it_and_prefills_the_answer(self):
        asked = self.say("Add a new team member", session="member")
        self.assertEqual(asked["speech"],
                         "What should the name of the new team member be? I'll open Teams and highlight Add member.")
        answered = self.say("Priya Shah", session="member", turn_id=2)
        self.assertEqual(self.action(answered), ("HIGHLIGHT_ADD_MEMBER_BUTTON", {"name": "Priya Shah"}))
        self.assertIsNone(answered["execution"], "a prepared form writes nothing")

    def test_opening_an_unavailable_persons_ticket_says_so_identically(self):
        elsewhere = self.say("open Maya's ticket", scope=PLATFORM, session="maya")["speech"]
        nobody = self.say("open Zed's ticket", scope=PLATFORM, session="zed")["speech"]
        self.assertEqual(elsewhere, "I can't find Maya in this workspace. I'll open Issues.")
        self.assertEqual(elsewhere.replace("Maya", "Zed"), nobody)

    def test_the_owners_wording_decisions(self):
        """5d plan revision 2, section 5: the four decisions, as the visitor hears them."""
        identity = self.say("who are you?", session="identity")["speech"]
        self.assertTrue(identity.startswith(f"I'm Edith, your guide to {PRODUCT_NAME}."), identity)
        self.assertNotIn('"', identity + self.say("what can you do?", session="caps")["speech"])
        route = self.say("Run the evaluator demo", session="route")["speech"]
        self.assertTrue(route.startswith(f"Here's a good way to explore {PRODUCT_NAME}: open "), route)
        self.assertIn(f"then ask me for something outside {PRODUCT_NAME} to see how I stay in scope.", route)
        self.assertEqual(self.say("not cycles, show me the issues", session="fix")["speech"],
                         "Got it. I'll switch to Issues.")
        self.assertEqual(self.say("show me the issues", session="plain")["speech"], "I'll open Issues.")
        self.assertEqual(self.say("make it high priority", session="which")["speech"],
                         "Which ticket do you mean? Open it first, or tell me which one.")

    def test_the_owners_production_conversation(self):
        """Replayed from the first production shadow run: each turn a visitor actually sent."""
        self.say("Assign it to Noah", session="prod")
        after = self.say("Show me issue assignment", session="prod", turn_id=2)
        self.assertEqual(self.action(after)[0], "HIGHLIGHT_ASSIGNMENT_CONTROL",
                         "a complete new request is never read as the answer to an open question")
        self.assertNotIn("Noah", after["speech"])
        for index, (message, name) in enumerate((("Hi there i am pushkar", "Pushkar"), ("hi i'm priya", "Priya"),
                                                 ("my name is sam", "Sam"), ("I'm Sam from Acme", "Sam"))):
            self.assertEqual(self.say(message, session=f"intro-{index}")["speech"],
                             f"Nice to meet you, {name}. What would you like to explore in {PRODUCT_NAME}?",
                             message)
        for index, message in enumerate(("i am confused", "i am looking for a tool")):
            self.assertNotIn("Nice to meet you", self.say(message, session=f"not-a-name-{index}")["speech"])
        capable = self.say("what is pixel capable of doing?", session="capable")
        self.assertTrue(capable["speech"].startswith(f"Here's what I can do in {PRODUCT_NAME}:"),
                        capable["speech"])
        self.assertIsNone(capable["execution"], "a question never proposes a change")
        self.assertEqual(self.say("who build pixel?", session="who")["speech"],
                         f"I don't have approved {PRODUCT_NAME} information to answer that, so I won't guess.")
        self.assertTrue(self.say("who is edith?", session="edith")["speech"].startswith("I'm Edith"))

    def test_an_unknown_person_is_offered_for_adding_in_every_request(self):
        """The last port row: an update naming someone unknown offers the add-member control."""
        self.say("Open Maya's ticket", session="add")
        body = self.say("assign it to Priya", session="add", turn_id=2)
        self.assertEqual(self.action(body), ("HIGHLIGHT_ADD_MEMBER_BUTTON", {"name": "Priya"}))
        self.assertEqual(body["speech"],
                         "I can't find Priya in this workspace. I'll open Teams and highlight Add member.")
        self.assertIsNone(body["execution"], "nothing is assigned to someone who is not there")

    def test_a_visitor_describing_their_situation_is_acknowledged(self):
        for index, message in enumerate(("i am new here", "we are just exploring", "I'm still looking around")):
            self.assertEqual(self.say(message, session=f"situation-{index}")["speech"],
                             f"Thanks, that helps. What would you like to explore first in {PRODUCT_NAME}?",
                             message)

    def test_a_question_about_pixel_without_approved_product_material_is_refused(self):
        for index, message in enumerate(("what is pixel?", "what does Pixel do?")):
            self.assertEqual(
                self.say(message, session=f"product-{index}")["speech"],
                f"I don't have approved {PRODUCT_NAME} information to answer that, so I won't guess.",
                message,
            )

    def test_a_request_that_needs_a_person_asks_instead_of_falling_back(self):
        """Found live: "assign it to priya" (an unrecognised name) answered "I'm not sure how to help"."""
        self.say("open Maya's ticket", session="ask")
        asked = self.say("assign it to priya", session="ask", turn_id=2)
        self.assertEqual(asked["speech"], "Who should LIN-142 be assigned to?")
        self.assertIsNone(asked["execution"])
        self.say("open Maya's ticket", session="named")
        named = self.say("assign it to Priya", session="named", turn_id=2)
        self.assertEqual(self.action(named), ("HIGHLIGHT_ADD_MEMBER_BUTTON", {"name": "Priya"}),
                         "a recognised name still offers the control that adds them")
        # With nothing open the missing record is asked for first, in both engines.
        self.assertEqual(self.say("assign it to Priya", session="nothing-open")["speech"],
                         "Which ticket do you mean? Open it first, or tell me which one.")

    def test_a_committed_receipt_names_the_record_it_points_at(self):
        """Found live: a created ticket's receipt said "project to PRJ-102"."""
        self.say("create a ticket for Noah about login errors", session="made")
        proposed = self.say("Issue Triage Workflow", session="made", turn_id=2)
        fields = dict(proposed["validated_action"]["payload"])
        headers = {**self.headers, "X-Execution-Key": proposed["execution"]["key"], "X-Session-Id": "made"}
        receipt = self.client.post("/api/demo-data/issues", json={"fields": fields}, headers=headers)
        self.assertEqual(receipt.status_code, 200, receipt.text)
        speech = receipt.json()["speech"]
        self.assertIn("project to Issue Triage Workflow", speech)
        self.assertNotIn("PRJ-", speech, "an identifier is evidence, never speech")

    def test_switching_workspace_keeps_the_conversation_alive(self):
        """Found live: every turn after a workspace switch answered "stale" and the chat died."""
        self.say("open Maya's ticket", session="switch")
        moved = self.say("how many team members are there", session="switch", turn_id=2, scope=PLATFORM)
        self.assertEqual(moved["status"], "completed")
        self.assertEqual(moved["speech"], "Platform Workspace has 2 team members. I'll open Teams.")
        again = self.say("show me the issues", session="switch", turn_id=3, scope=PLATFORM)
        self.assertEqual(again["status"], "completed")
        # The remembered ticket does not travel with the visitor into the other workspace.
        forgotten = self.say("assign it to Avery", session="switch", turn_id=4, scope=PLATFORM)
        self.assertNotIn("LIN-142", forgotten["speech"])

    def test_a_request_after_a_self_description_is_still_served(self):
        body = self.say("I'm a manager, show me the projects")
        self.assertEqual(self.action(body)[0], "OPEN_PROJECTS")


if __name__ == "__main__":
    unittest.main()
