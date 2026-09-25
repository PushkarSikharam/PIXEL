"""Moving around the application itself, by asking.

Somebody using Pixel is moving around a place, and asking to be taken somewhere is a request like
any other. These tests hold that to the same rules everything else follows: it is answered by the
engine from a definition, it belongs to one organization, and it never claims to do something the
application cannot.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_engine_cutover import EngineCutoverFixture  # noqa: E402

from app import main  # noqa: E402
from app.auth import create_token  # noqa: E402
from app.definitions.console import configured_console, ensure_console_product  # noqa: E402
from app.definitions.organizations import OrganizationDirectory  # noqa: E402
from app.definitions.registry import DefinitionRegistry  # noqa: E402

TENANT = "pixel-dev"
CONSOLE_DEFINITION = "pixel_console"


class ConsoleAssistantFixture(EngineCutoverFixture):
    def setUp(self):
        super().setUp()
        for patcher in (unittest.mock.patch.dict(os.environ, {
                            "PIXEL_ENGINE_MODE": "definition",
                            "PIXEL_CONSOLE_DEFINITION": CONSOLE_DEFINITION}),
                        unittest.mock.patch.object(main, "_authority", "definition")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.turns: dict[str, int] = {}
        self.directory = OrganizationDirectory(DefinitionRegistry())
        self.console = ensure_console_product(self.directory, TENANT, "planning-team",
                                              ("demo-admin",))

    def ask(self, message: str, session: str = "console") -> dict:
        self.turns[session] = self.turns.get(session, 0) + 1
        response = self.client.post("/api/turn", headers={
            "Authorization": f"Bearer {create_token('demo-admin', TENANT)}",
        }, json={"session_id": session, "turn_id": self.turns[session],
                 "product_id": self.console.product_id, "message": message,
                 "workspace_scope_id": "primary"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def action(body: dict) -> str | None:
        return (body.get("validated_action") or {}).get("type")


class MovingAroundTest(ConsoleAssistantFixture):
    def test_somebody_can_ask_to_be_taken_where_they_want_to_go(self):
        for message, expected in (
            ("take me to my products", "OPEN_PRODUCTS"),
            ("open members", "OPEN_MEMBERS"),
            ("show me people and teams", "OPEN_MEMBERS"),
            ("go to settings", "OPEN_SETTINGS"),
            ("take me home", "OPEN_OVERVIEW"),
        ):
            with self.subTest(message=message):
                self.assertEqual(self.action(self.ask(message, message[:8])), expected)

    def test_a_screen_pixel_does_not_have_is_declined_not_opened(self):
        """Test, Deploy, Operate and Audit were designs with nothing behind them; none is offered."""
        for message in ("show me the audit log", "open deployments", "open the test playground",
                        "show usage"):
            with self.subTest(message=message):
                body = self.ask(message, message[:10])
                self.assertIsNone(self.action(body))
                for word in ("audit", "deploy", "playground", "usage"):
                    self.assertNotIn(f"take you to {word}", body["speech"].lower())

    def test_asking_to_leave_where_you_are_takes_you_out(self):
        """Somebody inside a product says "take me out of this" and means it literally."""
        for message in ("take me out of this", "back to products", "leave this product"):
            with self.subTest(message=message):
                self.assertEqual(self.action(self.ask(message, message[:9])), "OPEN_PRODUCTS")

    def test_it_says_what_it_can_do_and_nothing_it_cannot(self):
        offered = self.ask("what can you do")["speech"]
        self.assertIn("Pixel", offered)
        for record_work in ("create", "change", "delete"):
            self.assertNotIn(record_work, offered.lower(),
                             "moving around is all it does; it must not offer record work")

    def test_it_says_how_many_products_the_organization_has(self):
        """Asked a number, it answers with the number rather than opening a screen and leaving
        somebody to count for themselves."""
        counted = self.ask("how many products do i have")
        self.assertRegex(counted["speech"], r"You have \d+ products?")
        self.assertIn("Pixel", counted["speech"], "it names them, not only counts them")

    def test_a_count_of_one_is_not_worded_as_many(self):
        answered = self.ask("how many products do i have", session="one")
        products = self.directory.active_products()
        mine = [b for b in products if b.tenant_id == TENANT and b.product_id != self.console.product_id]
        expected = f"You have {len(mine)} product" + ("s" if len(mine) != 1 else "")
        self.assertIn(expected, answered["speech"])

    def test_pixel_is_never_counted_among_somebodys_products(self):
        """Pixel is where the products are, not one of them."""
        counted = self.ask("how many products do i have", session="exclude")
        self.assertNotIn(self.console.product_id, counted["speech"])

    def test_it_says_how_many_people_are_here(self):
        counted = self.ask("how many people are in this organization")
        self.assertRegex(counted["speech"], r"You have \d+ (person|people)")

    def test_it_counts_only_this_organization(self):
        self.directory.create_organization("somebody-else", "Somebody Else")
        self.directory.add_member("somebody-else", "their-admin", "org_admin")
        counted = self.ask("how many people are in this organization", session="theirs")
        self.assertNotIn("their-admin", counted["speech"])

    def test_it_never_answers_about_a_product_it_does_not_hold(self):
        answered = self.ask("how many deals are there")
        self.assertIsNone(self.action(answered))
        self.assertNotIn("deal", answered["speech"].lower())

    def test_pixel_itself_is_not_listed_among_somebody_s_products(self):
        """Opening the application inside the application is not something to offer."""
        listed = self.client.get(f"/api/organizations/{TENANT}/products", headers={
            "Authorization": f"Bearer {create_token('demo-admin', TENANT)}"}).json()["products"]
        self.assertNotIn(self.console.product_id, [p["product_id"] for p in listed])
        self.assertIn("linear-demo", [p["product_id"] for p in listed])

    def test_it_refuses_destruction_like_everything_else(self):
        self.assertEqual(self.ask("delete everything")["status"], "denied")


class StageNameTest(unittest.TestCase):
    def test_the_response_contract_and_the_engine_agree_on_what_a_fallback_is(self):
        """`app.main` reads the stage without importing the engine, so the two names must match."""
        from app.engine.conversation_engine import TurnStage
        from app.schemas import FALLBACK_STAGE

        self.assertEqual(FALLBACK_STAGE, str(TurnStage.FALLBACK))


class WhoGetsOneTest(ConsoleAssistantFixture):
    def test_a_deployment_that_chose_none_has_none(self):
        with unittest.mock.patch.dict(os.environ, {"PIXEL_CONSOLE_DEFINITION": ""}):
            self.assertIsNone(configured_console())
            self.assertIsNone(ensure_console_product(self.directory, "other-org", "team"))

    def test_giving_it_twice_changes_nothing(self):
        again = ensure_console_product(self.directory, TENANT, "planning-team", ("demo-admin",))
        self.assertEqual(again, self.console)
        bound = self.directory.product(TENANT, self.console.product_id)
        self.assertEqual(bound.definition_id, CONSOLE_DEFINITION)

    def test_it_belongs_to_the_organization_that_was_given_it(self):
        self.directory.create_organization("somebody-else", "Somebody Else")
        self.directory.add_member("somebody-else", "their-admin", "org_admin")
        theirs = self.client.post("/api/turn", headers={
            "Authorization": f"Bearer {create_token('their-admin', 'somebody-else')}",
        }, json={"session_id": "theirs", "turn_id": 1, "product_id": self.console.product_id,
                 "message": "take me to my products", "workspace_scope_id": "primary"}).json()
        self.assertEqual(theirs["status"], "denied")

    def test_it_follows_the_platform_forward(self):
        """Moving around Pixel is Pixel's own, so every organization gets the current one
        rather than staying on whichever version existed when they signed up."""
        newest = max(self.directory.definitions.source.versions(CONSOLE_DEFINITION))
        self.assertGreater(newest, 1, "this only means anything with more than one version")
        self.directory.definitions.ensure_published(CONSOLE_DEFINITION, 1)
        self.directory.move_product_version(TENANT, self.console.product_id, 1)
        ensure_console_product(self.directory, TENANT, "planning-team", ("demo-admin",))
        self.assertEqual(self.directory.product(TENANT, self.console.product_id).definition_version,
                         newest)

    def test_a_customers_own_product_is_never_moved_this_way(self):
        bound = self.directory.product(TENANT, "linear-demo")
        ensure_console_product(self.directory, TENANT, "planning-team", ("demo-admin",))
        self.assertEqual(self.directory.product(TENANT, "linear-demo").definition_version,
                         bound.definition_version)


class AskingAboutPixelItselfTest(ConsoleAssistantFixture):
    """What the application is, answered from approved text rather than from a list of buttons.

    Somebody who asks how Pixel works is asking to understand it. Answering with the things they
    could click instead tells them nothing, and inventing an explanation would be worse; the
    platform ships documents about itself and quotes them.
    """

    def test_it_explains_how_the_application_works(self):
        answer = self.ask("how does Pixel work")
        self.assertIn("definition", answer["speech"].lower())
        self.assertIn("product documentation", answer["speech"])

    def test_what_is_pixel_is_not_answered_as_a_route_list(self):
        answer = self.ask("what is Pixel")
        self.assertIn("product documentation", answer["speech"])
        self.assertIn("product", answer["speech"].lower())
        self.assertNotIn("Here's what I can do", answer["speech"])
        self.assertIsNone(self.action(answer))

    def test_what_is_pixel_is_answered_from_the_page_that_says_what_it_is(self):
        """It used to quote "How Pixel works": checksums and pinned versions, to a newcomer."""
        for question in ("What is Pixel?", "what does Pixel do"):
            with self.subTest(question=question):
                speech = self.ask(question)["speech"]
                self.assertIn("Pixel is a platform for running products through conversation", speech)
                self.assertNotIn("checksum", speech)
                self.assertNotIn('"', speech)

    def test_how_pixel_works_is_still_answered_from_how_it_works(self):
        self.assertIn("Pixel works from definitions", self.ask("How does Pixel work?")["speech"])

    def test_another_tool_is_declined_politely_not_answered_with_a_list(self):
        for request in ("open salesforce", "check my gmail", "take me to hubspot"):
            with self.subTest(request=request):
                answer = self.ask(request, session=f"tool-{request}")
                self.assertIsNone(self.action(answer))
                self.assertIn("Pixel", answer["speech"])
                self.assertNotIn("I can open", answer["speech"])

    def test_the_people_screen_is_called_what_the_page_calls_it(self):
        answer = self.ask("take me to people", session="people")
        self.assertIn("People", answer["speech"])
        self.assertNotIn("Members", answer["speech"])

    def test_it_explains_what_happens_before_a_record_changes(self):
        answer = self.ask("how does Pixel keep my records safe")
        self.assertIn("confirm", answer["speech"].lower())

    def test_it_still_declines_what_it_has_no_text_for(self):
        answer = self.ask("what is the weather today")
        self.assertNotIn("product documentation", answer["speech"])
        self.assertIsNone(self.action(answer))

    def test_its_documents_belong_to_one_organization_and_one_version(self):
        binding = self.directory.product(TENANT, self.console.product_id)
        from app.engine.knowledge import KnowledgeContext
        from app.product_knowledge import ApprovedKnowledge
        context = KnowledgeContext(
            tenant_id=TENANT, product_id=self.console.product_id,
            definition_id=CONSOLE_DEFINITION, definition_version=binding.definition_version,
            definition_checksum=binding.definition_checksum,
            knowledge_version=binding.knowledge_version, scope_label="primary")
        self.assertTrue(ApprovedKnowledge(context).search("how does Pixel work"))
        from dataclasses import replace
        for change in ({"tenant_id": "another"}, {"product_id": "another"},
                       {"knowledge_version": 99}, {"definition_checksum": "f" * 64}):
            self.assertEqual(ApprovedKnowledge(replace(context, **change)).search("definition"), [])


class ShowingSomebodyRoundTest(ConsoleAssistantFixture):
    """Somebody who has just arrived asks to be shown round, and gets a route rather than a
    fallback. The route starts where the product's author declared first, which for Pixel is
    adding a product: the reason somebody is here at all."""

    def test_asking_to_be_shown_round_gives_a_route(self):
        for phrase in ("show me around", "give me a guided tour", "walk me through Pixel"):
            with self.subTest(phrase=phrase):
                speech = self.ask(phrase, session=f"tour-{phrase}")["speech"]
                self.assertIn("New product", speech)
                self.assertIn("then", speech)

    def test_the_route_only_names_places_that_exist(self):
        speech = self.ask("show me around")["speech"]
        labels = {view.label for view in
                  self.directory.definitions.load(CONSOLE_DEFINITION,
                                                  self.directory.product(TENANT, self.console.product_id)
                                                  .definition_version).definition.views.values()}
        named = [label for label in labels if f"open {label}" in speech]
        self.assertTrue(named, speech)


class ShippedDocumentsTest(ConsoleAssistantFixture):
    """Publishing a package's own documents, on the same terms as text somebody approves."""

    def binding(self):
        return self.directory.product(TENANT, self.console.product_id)

    def test_publishing_twice_does_not_add_a_version(self):
        from app.definitions.shipped_knowledge import publish_shipped_documents
        before = self.binding().knowledge_version
        again = publish_shipped_documents(
            self.directory.definitions.source, TENANT, self.console.product_id,
            CONSOLE_DEFINITION, self.binding().definition_checksum, before)
        self.assertEqual(again, before)
        self.assertEqual(self.binding().knowledge_version, before)

    def test_text_somebody_approved_is_never_replaced(self):
        from app.db import get_connection
        from app.definitions.shipped_knowledge import publish_shipped_documents
        binding = self.binding()
        with get_connection() as connection:
            connection.execute("insert into approved_documents values (?, ?, ?, ?, ?, ?, ?)",
                               (TENANT, self.console.product_id, binding.definition_checksum,
                                binding.knowledge_version, "0123456789abcdef",
                                "Our own note", "Something we wrote ourselves."))
        unchanged = publish_shipped_documents(
            self.directory.definitions.source, TENANT, self.console.product_id,
            CONSOLE_DEFINITION, binding.definition_checksum, binding.knowledge_version)
        self.assertEqual(unchanged, binding.knowledge_version)
        with get_connection() as connection:
            kept = connection.execute(
                "select 1 from approved_documents where document_id=? and knowledge_version=?",
                ("0123456789abcdef", binding.knowledge_version)).fetchone()
        self.assertIsNotNone(kept)

    def test_a_package_that_ships_no_documents_publishes_nothing(self):
        from app.definitions.shipped_knowledge import publish_shipped_documents
        binding = self.binding()
        version = publish_shipped_documents(
            self.directory.definitions.source, TENANT, self.console.product_id,
            "linear_simplified", binding.definition_checksum, binding.knowledge_version)
        self.assertEqual(version, binding.knowledge_version)


class WhoIsInThisOrganizationTest(ConsoleAssistantFixture):
    """The people screen has to show the people who are really there.

    A sample list is the right thing when the console has no Pixel behind it, and the wrong thing
    the moment it does: somebody signing in to see who is in their workspace must not be shown
    colleagues who do not exist, and must never be shown another organization's.
    """

    def members(self, user: str = "demo-admin", tenant: str = TENANT):
        return self.client.get(f"/api/organizations/{tenant}/members", headers={
            "Authorization": f"Bearer {create_token(user, tenant)}"})

    def test_it_lists_this_organization(self):
        found = self.members()
        self.assertEqual(found.status_code, 200, found.text)
        listed = {member["user_id"] for member in found.json()["members"]}
        self.assertIn("demo-admin", listed)
        self.assertEqual(found.json()["tenant_id"], TENANT)

    def test_every_person_carries_their_role_and_team(self):
        for member in self.members().json()["members"]:
            self.assertTrue(member["role"])
            if member["team_id"]:
                self.assertTrue(member["team_name"], member)

    def test_another_organization_is_not_available(self):
        self.directory.create_organization("somebody-else", "Somebody Else")
        self.directory.add_member("somebody-else", "their-admin", "org_admin")
        theirs = self.client.get(f"/api/organizations/{TENANT}/members", headers={
            "Authorization": f"Bearer {create_token('their-admin', 'somebody-else')}"})
        self.assertEqual(theirs.status_code, 404, theirs.text)
        mine = self.members("their-admin", "somebody-else")
        self.assertEqual({m["user_id"] for m in mine.json()["members"]}, {"their-admin"})

    def test_it_agrees_with_what_the_assistant_counts(self):
        spoken = self.ask("how many people are in my organization")["speech"]
        self.assertIn(str(len(self.members().json()["members"])), spoken)


class GoingBackTest(ConsoleAssistantFixture):
    """Going back means the screen somebody was on.

    A product can say which of its screens a phrase means; it cannot know where this person has
    been. So a bare request to go back is answered by the platform from the conversation itself,
    and a phrase that names a destination still goes to that destination.
    """

    def test_it_returns_to_the_screen_before_this_one(self):
        self.ask("open products", session="back")
        self.ask("open members", session="back")
        self.assertIn("Products", self.ask("go back", session="back")["speech"])

    def test_going_back_twice_returns_to_where_that_started(self):
        self.ask("open products", session="twice")
        self.ask("open members", session="twice")
        self.assertIn("Products", self.ask("go back", session="twice")["speech"])
        self.assertIn("People", self.ask("go back", session="twice")["speech"])

    def test_a_named_destination_is_still_that_destination(self):
        self.ask("open members", session="named")
        self.assertIn("Products", self.ask("back to my products", session="named")["speech"])

    def test_with_nowhere_to_go_back_to_the_product_decides(self):
        """The first thing somebody says has no screen behind it, so the definition answers."""
        answer = self.ask("go back", session="first")
        self.assertIsNotNone(self.action(answer))
        self.assertIn("open", answer["speech"].lower())


class NobodyIsLeftBehindTest(ConsoleAssistantFixture):
    """Every organization runs the assistant this deployment ships.

    Setting the console product up only when an account was created left organizations on
    whatever version existed the day they signed up. There was no symptom anyone could see
    either: the assistant answered every question in the wording of its own version, which reads
    as an assistant that does not work rather than one that is old.
    """

    def newest(self) -> int:
        return max(self.directory.definitions.source.versions(CONSOLE_DEFINITION))

    def put_behind(self, version: int = 1) -> None:
        self.directory.definitions.ensure_published(CONSOLE_DEFINITION, version)
        self.directory.move_product_version(TENANT, self.console.product_id, version)

    def test_no_organization_is_behind_the_version_we_ship(self):
        """The check that stops this happening again."""
        from app.definitions.console import console_versions_behind

        self.assertEqual(console_versions_behind(self.directory), {})

    def test_an_organization_left_behind_is_reported(self):
        from app.definitions.console import console_versions_behind

        self.put_behind()
        self.assertEqual(console_versions_behind(self.directory), {TENANT: 1})

    def test_the_catch_up_brings_it_forward(self):
        from app.definitions.console import catch_up_console_products, console_versions_behind

        self.put_behind()
        self.assertEqual(catch_up_console_products(self.directory), 1)
        self.assertEqual(console_versions_behind(self.directory), {})
        self.assertEqual(self.directory.product(TENANT, self.console.product_id).definition_version,
                         self.newest())

    def test_catching_up_leaves_a_customers_own_product_alone(self):
        from app.definitions.console import catch_up_console_products

        before = self.directory.product(TENANT, "linear-demo")
        self.put_behind()
        catch_up_console_products(self.directory)
        after = self.directory.product(TENANT, "linear-demo")
        self.assertEqual(after.definition_version, before.definition_version)
        self.assertEqual(after.definition_id, before.definition_id)

    def test_catching_up_leaves_the_organization_and_its_people_alone(self):
        from app.definitions.console import catch_up_console_products

        organization = self.directory.organization(TENANT)
        members = {(m.user_id, m.role, m.team_id) for m in self.directory.members(TENANT)}
        teams = {(t.team_id, t.name, t.state) for t in self.directory.teams(TENANT)}
        self.put_behind()
        catch_up_console_products(self.directory)
        self.assertEqual(self.directory.organization(TENANT), organization)
        self.assertEqual({(m.user_id, m.role, m.team_id) for m in self.directory.members(TENANT)}, members)
        self.assertEqual({(t.team_id, t.name, t.state) for t in self.directory.teams(TENANT)}, teams)

    def test_an_account_left_behind_answers_like_the_newest_once_it_catches_up(self):
        """The symptom, gone: the same question, before and after."""
        from app.definitions.console import catch_up_console_products

        self.put_behind()
        behind = self.ask("how many products do I have", session="behind")["speech"]
        self.assertNotIn("You have", behind)
        catch_up_console_products(self.directory)
        caught_up = self.ask("how many products do I have", session="caught-up")["speech"]
        self.assertIn("You have", caught_up)


if __name__ == "__main__":
    unittest.main()
