"""A product added to Pixel, served through the same API as the one this repository ships (3.5).

This is the path a person takes: they give Pixel a product, Pixel keeps it, and Edith answers for
it. Nothing about that product is a file here and nothing about it is Python. It is a definition
in the database, records in the store, and a binding that says this organization runs it.

The demo product runs beside it throughout, because "nothing overlaps" is the requirement these
tests exist to hold.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_engine_cutover import EngineCutoverFixture  # noqa: E402

from app import main  # noqa: E402

from app.auth import create_token  # noqa: E402
from app.definitions.authoring import store_definition  # noqa: E402
from app.definitions.console import ensure_console_product  # noqa: E402
from app.definitions.loader import DefinitionError  # noqa: E402
from app.definitions.organizations import OrganizationDirectory  # noqa: E402
from app.definitions.registry import DefinitionRegistry  # noqa: E402
from app.record_access import grant_records  # noqa: E402
from app.services.record_store import PRIMARY, RecordStore  # noqa: E402
from library_fixtures import library_definition  # noqa: E402

TENANT, PRODUCT, DEFINITION = "pixel-dev", "an-added-product", "sample_library"
DEMO, DEMO_SCOPE = "linear-demo", "workspace-product-eng"


class AddedProductFixture(EngineCutoverFixture):
    def setUp(self):
        super().setUp()
        # The authority is fixed when the application starts, so a test module that ran the
        # lifespan earlier can leave it pinned. Both are set here, so this suite states the
        # authority it tests under rather than inheriting whatever ran before it.
        for patcher in (unittest.mock.patch.dict(os.environ, {"PIXEL_ENGINE_MODE": "definition"}),
                        unittest.mock.patch.object(main, "_authority", "definition")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.registry = DefinitionRegistry()
        self.directory = OrganizationDirectory(self.registry)
        self.turns: dict[str, int] = {}

    def add_product(self, version: int = 1) -> None:
        """Everything adding a product does, with no file and no code anywhere in it."""
        document = library_definition(version)
        store_definition(yaml.safe_dump(document, sort_keys=False),
                         definition_id=DEFINITION, version=version)
        self.registry.ensure_published(DEFINITION, version)
        self.directory.bind_product(TENANT, PRODUCT, "planning-team", DEFINITION, version)
        grant_records(TENANT, PRODUCT, "demo-admin", [], True)
        self.definition = self.registry.load(DEFINITION, version).definition
        self.store = RecordStore(TENANT, PRODUCT, PRIMARY)

    def add_records(self) -> None:
        self.rosa = self.store.create(self.definition, "librarian", {"name": "Rosa Vale"})
        self.otto = self.store.create(self.definition, "librarian", {"name": "Otto Lind"})
        self.tide = self.store.create(self.definition, "book", {
            "title": "Tide Tables", "status": "On shelf", "keeper": self.rosa.id})
        self.charts = self.store.create(self.definition, "book", {
            "title": "Harbour Charts", "status": "On loan", "keeper": self.otto.id})

    def ask(self, message: str, *, product: str = PRODUCT, scope: str = PRIMARY,
            session: str = "added", current_page: str | None = None) -> dict:
        """One turn of a conversation. Messages sharing a session continue it, as a visitor's do."""
        self.turns[session] = self.turns.get(session, 0) + 1
        response = self.client.post("/api/turn", headers={
            "Authorization": f"Bearer {create_token('demo-admin', TENANT)}",
        }, json={
            "session_id": session, "turn_id": self.turns[session], "product_id": product,
            "message": message, "workspace_scope_id": scope,
            **({"current_page": current_page} if current_page else {}),
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def action(body: dict) -> str | None:
        return (body.get("validated_action") or {}).get("type")


class AddingAProductTest(AddedProductFixture):
    def test_a_product_that_is_only_a_definition_answers_through_the_api(self):
        self.add_product()
        self.add_records()
        opened = self.ask("show me the books")
        self.assertEqual(self.action(opened), "OPEN_CATALOGUE")
        counted = self.ask("how many books are there")
        self.assertIn("Tide Tables", counted["speech"])
        found = self.ask("books for Rosa")
        self.assertEqual(self.action(found), "BOOKS_BY_KEEPER")
        self.assertIn("Rosa Vale", found["speech"])
        one = self.ask(f"open {self.tide.id}")
        self.assertEqual(self.action(one), "OPEN_BOOK")

    def test_it_offers_only_what_its_own_definition_declares(self):
        self.add_product()
        self.add_records()
        offered = self.ask("what can you do")["speech"]
        self.assertIn("Sample Library", offered)
        self.assertIn("book", offered)
        self.assertNotIn("ticket", offered)

    def test_it_refuses_by_its_own_guardrail(self):
        self.add_product()
        self.add_records()
        refused = self.ask("delete everything")
        self.assertEqual(refused["status"], "denied")
        self.assertIsNone(self.action(refused))

    def test_it_never_speaks_an_identifier_the_platform_uses_internally(self):
        self.add_product()
        self.add_records()
        spoken = self.ask("how many books are there")["speech"]
        self.assertNotIn(PRIMARY, spoken)
        self.assertNotIn(DEFINITION, spoken)

    def test_the_added_product_and_the_shipped_one_do_not_meet(self):
        self.add_product()
        self.add_records()
        self.assertIsNone(self.action(self.ask("show me the books", product=DEMO, scope=DEMO_SCOPE)))
        self.assertIsNone(self.action(self.ask("assign LIN-142 to Noah")))
        demo = self.ask("how many tickets are there", product=DEMO, scope=DEMO_SCOPE)
        self.assertEqual(self.action(demo), "OPEN_ISSUES")
        self.assertNotIn("Tide Tables", demo["speech"])

    def test_what_was_added_is_still_there_for_a_later_conversation(self):
        """Somebody comes back: their product and its records are where they left them."""
        self.add_product()
        self.add_records()
        later = OrganizationDirectory(DefinitionRegistry())
        self.assertEqual(later.product(TENANT, PRODUCT).definition_id, DEFINITION)
        self.assertEqual(len(RecordStore(TENANT, PRODUCT, PRIMARY).all()["book"]), 2)
        self.assertIn("Tide Tables", self.ask("how many books are there", session="later")["speech"])


class ChangingRecordsThroughTheChatTest(AddedProductFixture):
    """The demo's own workflow, on a product Pixel was handed: propose, then carry out."""

    def write(self, turn: dict, *, entity: str) -> dict:
        """Do what a client does with a proposal: present the key and exactly the bound change."""
        action = turn["validated_action"]
        payload = dict(action["payload"])
        key = turn["execution"]["key"]
        headers = {"Authorization": f"Bearer {create_token('demo-admin', TENANT)}",
                   "X-Execution-Key": key, "X-Session-Id": turn["execution"]["session_id"]}
        action_key = action["type"].lower()
        record_id = payload.pop("record_id", None)
        if record_id is None:
            return self.client.post(f"/api/products/{PRODUCT}/records/{entity}", headers=headers,
                                    json={"action": action_key, "fields": payload})
        return self.client.patch(
            f"/api/products/{PRODUCT}/records/{entity}/{record_id}", headers=headers,
            json={"action": action_key, "changes": payload})

    def test_a_record_is_created_through_the_chat_and_stays(self):
        self.add_product()
        self.add_records()
        asked = self.ask("add a book for Rosa", session="create")
        self.assertIsNone(self.action(asked), "a create missing a field asks, never guesses")
        self.assertIn("title", asked["speech"])
        proposed = self.ask("Reef Guide", session="create")
        self.assertEqual(self.action(proposed), "ADD_BOOK")
        self.assertEqual(proposed["validated_action"]["payload"]["title"], "Reef Guide")
        self.assertEqual(proposed["validated_action"]["payload"]["status"], "On shelf",
                         "a field the definition defaults is filled from the definition")
        self.assertTrue(proposed["speech"].startswith("I'll"), "proposed, never claimed as done")
        self.assertIsNotNone(proposed["execution"])
        receipt = self.write(proposed, entity="book")
        self.assertEqual(receipt.status_code, 200, receipt.text)
        titles = [record.fields["title"] for record in self.store.all()["book"]]
        self.assertIn("Reef Guide", titles)

    def test_a_record_is_assigned_to_a_person_through_the_chat(self):
        self.add_product()
        self.add_records()
        self.ask(f"open {self.tide.id}", session="assign")
        proposed = self.ask("give it to Otto", session="assign")
        self.assertEqual(self.action(proposed), "REASSIGN_BOOK")
        self.assertEqual(proposed["validated_action"]["payload"]["keeper"], self.otto.id)
        receipt = self.write(proposed, entity="book")
        self.assertEqual(receipt.status_code, 200, receipt.text)
        self.assertEqual(self.store.get("book", self.tide.id).fields["keeper"], self.otto.id)

    def test_a_person_is_added_through_the_chat(self):
        """Adding a person is a create like any other, and asks for what it needs."""
        self.add_product()
        self.add_records()
        asked = self.ask("add a librarian", session="member")
        self.assertIsNone(self.action(asked))
        self.assertIn("name", asked["speech"])
        proposed = self.ask("Pia Moreau", session="member")
        self.assertEqual(self.action(proposed), "ADD_LIBRARIAN")
        receipt = self.write(proposed, entity="librarian")
        self.assertEqual(receipt.status_code, 200, receipt.text)
        self.assertIn("Pia Moreau", [r.fields["name"] for r in self.store.all()["librarian"]])

    def test_a_create_asks_rather_than_reading_it_as_a_change(self):
        """Adding something is not changing something: the question must be about the create."""
        self.add_product()
        self.add_records()
        asked = self.ask("add a librarian", session="not-a-change")
        self.assertNotIn("change", asked["speech"].lower())

    def test_the_same_key_never_writes_twice(self):
        self.add_product()
        self.add_records()
        self.ask(f"open {self.tide.id}", session="once")
        proposed = self.ask("give it to Otto", session="once")
        self.assertEqual(self.write(proposed, entity="book").status_code, 200)
        again = self.write(proposed, entity="book")
        self.assertEqual(again.status_code, 200, again.text)
        self.assertIn("already", again.json()["speech"].lower())
        self.assertEqual(self.store.get("book", self.tide.id).revision, 2, "one write, not two")

    def test_a_change_the_key_did_not_bind_is_refused(self):
        self.add_product()
        self.add_records()
        self.ask(f"open {self.tide.id}", session="tamper")
        proposed = self.ask("give it to Otto", session="tamper")
        headers = {"Authorization": f"Bearer {create_token('demo-admin', TENANT)}",
                   "X-Execution-Key": proposed["execution"]["key"],
                   "X-Session-Id": proposed["execution"]["session_id"]}
        tampered = self.client.patch(
            f"/api/products/{PRODUCT}/records/book/{self.tide.id}", headers=headers,
            json={"action": "reassign_book", "changes": {"status": "On loan"}})
        self.assertEqual(tampered.status_code, 409, tampered.text)
        self.assertEqual(self.store.get("book", self.tide.id).fields["keeper"], self.rosa.id)

    def test_leaving_a_conversation_withdraws_what_it_proposed(self):
        """A change nobody carried out must not stay usable once somebody has moved on."""
        self.add_product()
        self.add_records()
        self.ask(f"open {self.tide.id}", session="left")
        proposed = self.ask("give it to Otto", session="left")
        self.assertIsNotNone(proposed["execution"])
        closed = self.client.post("/api/conversations/left/close", headers={
            "Authorization": f"Bearer {create_token('demo-admin', TENANT)}"})
        self.assertEqual(closed.status_code, 200, closed.text)
        self.assertEqual(closed.json()["withdrawn"], 1)
        refused = self.write(proposed, entity="book")
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(self.store.get("book", self.tide.id).fields["keeper"], self.rosa.id)

    def test_closing_somebody_elses_conversation_is_not_possible(self):
        self.add_product()
        self.add_records()
        self.ask("show me the books", session="mine")
        self.directory.create_organization("somebody-else", "Somebody Else")
        self.directory.add_member("somebody-else", "their-admin", "org_admin")
        refused = self.client.post("/api/conversations/mine/close", headers={
            "Authorization": f"Bearer {create_token('their-admin', 'somebody-else')}"})
        self.assertEqual(refused.status_code, 404, refused.text)

    def test_a_write_without_a_key_is_not_accepted_at_all(self):
        self.add_product()
        self.add_records()
        response = self.client.patch(
            f"/api/products/{PRODUCT}/records/book/{self.tide.id}",
            headers={"Authorization": f"Bearer {create_token('demo-admin', TENANT)}"},
            json={"action": "reassign_book", "changes": {"status": "On loan"}})
        self.assertEqual(response.status_code, 422, response.text)

    def test_a_record_of_another_product_is_not_reachable_through_this_one(self):
        self.add_product()
        self.add_records()
        self.ask(f"open {self.tide.id}", session="cross")
        proposed = self.ask("give it to Otto", session="cross")
        headers = {"Authorization": f"Bearer {create_token('demo-admin', TENANT)}",
                   "X-Execution-Key": proposed["execution"]["key"],
                   "X-Session-Id": proposed["execution"]["session_id"]}
        elsewhere = self.client.patch(
            f"/api/products/{DEMO}/records/book/{self.tide.id}", headers=headers,
            json={"action": "reassign_book", "changes": {"keeper": self.otto.id}})
        self.assertEqual(elsewhere.status_code, 404, elsewhere.text)

    def test_what_the_chat_changed_is_what_the_chat_reports(self):
        self.add_product()
        self.add_records()
        self.ask(f"open {self.tide.id}", session="ledger")
        proposed = self.ask("give it to Otto", session="ledger")
        self.assertEqual(self.write(proposed, entity="book").status_code, 200)
        self.assertIn(self.tide.id, self.ask("what changed", session="ledger")["speech"])


class LeavingAProductTest(AddedProductFixture):
    """Asking to leave where you are is navigation, not a question the product must answer."""

    def setUp(self):
        super().setUp()
        console = unittest.mock.patch.dict(os.environ, {"PIXEL_CONSOLE_DEFINITION": "pixel_console"})
        console.start()
        self.addCleanup(console.stop)
        self.console = ensure_console_product(self.directory, TENANT, "planning-team", ("demo-admin",))
        self.add_product()
        self.add_records()

    def test_asking_to_leave_from_inside_a_product_takes_you_out(self):
        for message in ("take me back to my products", "open my products", "take me out of this"):
            with self.subTest(message=message):
                answered = self.ask(message, session=f"leave-{message[:10]}")
                self.assertEqual(self.action(answered), "OPEN_PRODUCTS", answered["speech"])

    def test_leaving_works_from_whichever_screen_is_open(self):
        """The console sends the product screen that is open. Passed on to the application, which
        has no such screen, it made leaving fail from every product page in a real browser."""
        answered = self.ask("take me back to my products", session="from-a-screen", current_page="catalogue")
        self.assertEqual(self.action(answered), "OPEN_PRODUCTS", answered["speech"])

    def test_the_product_still_answers_its_own_questions(self):
        """Nothing the product can answer is handed to the application instead."""
        self.assertEqual(self.action(self.ask("show me the books", session="own")), "OPEN_CATALOGUE")
        counted = self.ask("how many books are there", session="own")
        self.assertIn("Tide Tables", counted["speech"])

    def test_leaving_does_not_join_the_two_conversations(self):
        """The product's own memory must not gain anything from a turn the application answered."""
        self.ask(f"open {self.tide.id}", session="apart")
        self.ask("take me back to my products", session="apart")
        # The record opened before is still what "it" refers to in the product's conversation.
        changed = self.ask("give it to Otto", session="apart")
        self.assertEqual(self.action(changed), "REASSIGN_BOOK")
        self.assertEqual(changed["validated_action"]["payload"]["record_id"], self.tide.id)

    def test_the_turn_is_reported_in_the_session_the_caller_is_in(self):
        answered = self.ask("take me back to my products", session="reported")
        self.assertEqual(answered["session_id"], "reported")

    def test_nothing_the_product_could_not_answer_is_invented(self):
        """A request that is neither the product's nor the application's is still unanswered."""
        lost = self.ask("arrange a taxi for tomorrow morning", session="neither")
        self.assertIsNone(self.action(lost))

    def test_an_organization_without_the_application_product_keeps_the_product_reply(self):
        with unittest.mock.patch.dict(os.environ, {"PIXEL_CONSOLE_DEFINITION": ""}):
            answered = self.ask("take me back to my products", session="none")
            self.assertIsNone(self.action(answered))


class ProductEndpointsTest(AddedProductFixture):
    """What the console calls: list this organization's products, and add one."""

    def as_user(self, user_id: str = "demo-admin", tenant_id: str = TENANT) -> dict:
        return {"Authorization": f"Bearer {create_token(user_id, tenant_id)}"}

    def test_listing_shows_this_organizations_products_and_what_they_can_hold(self):
        self.add_product()
        response = self.client.get(f"/api/organizations/{TENANT}/products", headers=self.as_user())
        self.assertEqual(response.status_code, 200, response.text)
        listed = {product["product_id"]: product for product in response.json()["products"]}
        self.assertIn(DEMO, listed)
        self.assertEqual(listed[PRODUCT]["name"], "Sample Library")
        self.assertEqual(listed[PRODUCT]["entities"], ["book", "librarian"])
        self.assertEqual(listed[PRODUCT]["views"], ["catalogue", "librarians"])

    def test_a_member_of_another_organization_sees_nothing_of_this_one(self):
        self.add_product()
        self.directory.create_organization("somebody-else", "Somebody Else")
        self.directory.add_member("somebody-else", "their-admin", "org_admin")
        theirs = self.as_user("their-admin", "somebody-else")
        refused = self.client.get(f"/api/organizations/{TENANT}/products", headers=theirs)
        self.assertEqual(refused.status_code, 404, refused.text)
        # Their own organization answers, and holds nothing of ours.
        own = self.client.get("/api/organizations/somebody-else/products", headers=theirs)
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual(own.json()["products"], [])

    def test_a_token_cannot_even_be_made_for_an_organization_you_are_not_in(self):
        with self.assertRaises(ValueError):
            create_token("demo-admin", "somebody-else")

    def test_a_visitor_cannot_list_products_at_all(self):
        response = self.client.get(f"/api/organizations/{TENANT}/products")
        self.assertEqual(response.status_code, 401, response.text)

    def test_adding_a_product_makes_it_answer_straight_away(self):
        definition = library_definition()
        definition["definition"].update(ownership="organization_private", owner_organization=TENANT)
        response = self.client.post(f"/api/organizations/{TENANT}/products", headers=self.as_user(), json={
            "product_id": "added-through-the-api", "team_id": "planning-team",
            "definition_id": DEFINITION,
            "definition": yaml.safe_dump(definition, sort_keys=False),
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["name"], "Sample Library")
        answered = self.ask("what can you do", product="added-through-the-api")
        self.assertIn("Sample Library", answered["speech"])

    def test_a_team_admin_may_add_a_product_for_their_team(self):
        definition = library_definition()
        definition["definition"].update(ownership="organization_private", owner_organization=TENANT)
        self.directory.add_member(TENANT, "team-owner", "team_admin", "planning-team")
        response = self.client.post(f"/api/organizations/{TENANT}/products",
                                    headers=self.as_user("team-owner"), json={
            "product_id": "added-by-team-owner", "team_id": "planning-team",
            "definition_id": DEFINITION,
            "definition": yaml.safe_dump(definition, sort_keys=False),
        })
        self.assertEqual(response.status_code, 201, response.text)

    def test_a_team_admin_cannot_add_a_product_for_another_team(self):
        self.directory.create_team(TENANT, "platform-team", "Platform")
        self.directory.add_member(TENANT, "team-owner", "team_admin", "planning-team")
        response = self.client.post(f"/api/organizations/{TENANT}/products",
                                    headers=self.as_user("team-owner"), json={
            "product_id": "wrong-team-product", "team_id": "platform-team",
            "definition_id": DEFINITION,
            "definition": yaml.safe_dump(library_definition(), sort_keys=False),
        })
        self.assertEqual(response.status_code, 403, response.text)

    def test_a_definition_that_is_not_one_is_refused_and_nothing_is_added(self):
        response = self.client.post(f"/api/organizations/{TENANT}/products", headers=self.as_user(), json={
            "product_id": "never-added", "team_id": "planning-team",
            "definition_id": "not_a_definition", "definition": "this: is not a product",
        })
        self.assertEqual(response.status_code, 400, response.text)
        listed = self.client.get(f"/api/organizations/{TENANT}/products", headers=self.as_user())
        self.assertNotIn("never-added", [p["product_id"] for p in listed.json()["products"]])

    def test_only_an_administrator_may_add_a_product(self):
        self.directory.add_member(TENANT, "not-an-admin", "team_member", "planning-team")
        response = self.client.post(f"/api/organizations/{TENANT}/products",
                                    headers=self.as_user("not-an-admin"), json={
            "product_id": "should-not-exist", "team_id": "planning-team",
            "definition_id": DEFINITION,
            "definition": yaml.safe_dump(library_definition(), sort_keys=False),
        })
        self.assertEqual(response.status_code, 403, response.text)


class RenderingAnUnknownProductTest(AddedProductFixture):
    """What a screen needs to draw a product nobody wrote code for."""

    def as_user(self, user_id: str = "demo-admin", tenant_id: str = TENANT) -> dict:
        return {"Authorization": f"Bearer {create_token(user_id, tenant_id)}"}

    def test_the_shape_says_what_the_screens_and_things_are_called(self):
        self.add_product()
        response = self.client.get(f"/api/products/{PRODUCT}/shape", headers=self.as_user())
        self.assertEqual(response.status_code, 200, response.text)
        shape = response.json()
        self.assertEqual((shape["product_name"], shape["assistant_name"]), ("Sample Library", "Guide"))
        views = {view["name"]: view for view in shape["views"]}
        self.assertEqual(views["catalogue"]["label"], "Catalogue")
        self.assertEqual(views["catalogue"]["columns"], ["title", "status"])
        entities = {entity["name"]: entity for entity in shape["entities"]}
        fields = {field["name"]: field for field in entities["book"]["fields"]}
        self.assertEqual(fields["status"]["values"], ["On shelf", "On loan"])
        self.assertEqual(fields["keeper"]["target"], "librarian")
        self.assertFalse(entities["librarian"]["fields"][0]["editable"],
                         "a screen must know what it may not offer to change")

    def test_the_shape_carries_no_records(self):
        self.add_product()
        self.add_records()
        shape = self.client.get(f"/api/products/{PRODUCT}/shape", headers=self.as_user()).text
        self.assertNotIn("Tide Tables", shape)

    def test_records_come_back_grouped_by_the_kind_of_thing_they_are(self):
        self.add_product()
        self.add_records()
        response = self.client.get(f"/api/products/{PRODUCT}/records", headers=self.as_user())
        self.assertEqual(response.status_code, 200, response.text)
        records = response.json()["records"]
        self.assertEqual({record["title"] for record in records["book"]},
                         {"Tide Tables", "Harbour Charts"})
        self.assertEqual({record["name"] for record in records["librarian"]},
                         {"Rosa Vale", "Otto Lind"})

    def test_a_product_of_another_organization_has_no_shape_to_read(self):
        self.add_product()
        self.directory.create_organization("somebody-else", "Somebody Else")
        self.directory.add_member("somebody-else", "their-admin", "org_admin")
        theirs = self.as_user("their-admin", "somebody-else")
        self.assertEqual(
            self.client.get(f"/api/products/{PRODUCT}/shape", headers=theirs).status_code, 404)
        self.assertEqual(
            self.client.get(f"/api/products/{PRODUCT}/records", headers=theirs).status_code, 404)

    def test_what_the_chat_changes_is_what_the_screens_then_read(self):
        """One product, one set of records: the chat and the screens never disagree."""
        self.add_product()
        self.add_records()
        self.ask(f"open {self.tide.id}", session="screens")
        proposed = self.ask("give it to Otto", session="screens")
        headers = {**self.as_user(), "X-Execution-Key": proposed["execution"]["key"],
                   "X-Session-Id": proposed["execution"]["session_id"]}
        receipt = self.client.patch(
            f"/api/products/{PRODUCT}/records/book/{self.tide.id}", headers=headers,
            json={"action": "reassign_book",
                  "changes": {"keeper": proposed["validated_action"]["payload"]["keeper"]}})
        self.assertEqual(receipt.status_code, 200, receipt.text)
        read = self.client.get(f"/api/products/{PRODUCT}/records", headers=self.as_user()).json()
        changed = next(r for r in read["records"]["book"] if r["id"] == self.tide.id)
        self.assertEqual(changed["keeper"], self.otto.id)


class StoredDefinitionsAreStillDefinitionsTest(AddedProductFixture):
    def test_a_definition_that_does_not_parse_is_never_stored(self):
        with self.assertRaises(DefinitionError):
            store_definition("not: [a, definition", definition_id=DEFINITION, version=1)
        self.assertIsNone(DefinitionRegistry().get(DEFINITION, 1))

    def test_a_definition_must_declare_the_identity_it_is_stored_under(self):
        text = yaml.safe_dump(library_definition(), sort_keys=False)
        with self.assertRaises(DefinitionError) as refused:
            store_definition(text, definition_id="something_else", version=1)
        self.assertIn("declares", str(refused.exception))

    def test_a_stored_version_is_never_quietly_replaced(self):
        self.add_product()
        changed = library_definition()
        changed["identity"]["product_name"] = "Renamed Behind Your Back"
        with self.assertRaises(DefinitionError) as refused:
            store_definition(yaml.safe_dump(changed, sort_keys=False),
                             definition_id=DEFINITION, version=1)
        self.assertIn("new version", str(refused.exception))
        self.assertEqual(self.registry.load(DEFINITION, 1).definition.identity.product_name,
                         "Sample Library")

    def test_storing_the_same_text_again_is_not_an_error(self):
        self.add_product()
        same = yaml.safe_dump(library_definition(), sort_keys=False)
        self.assertEqual(store_definition(same, definition_id=DEFINITION, version=1),
                         self.registry.get(DEFINITION, 1).checksum)


class FillingInAFormTest(AddedProductFixture):
    """Somebody adds and edits records themselves, without asking the assistant for them.

    The keyed path exists because a change the assistant proposed was written by a model, so its
    parameters have to be bound to a confirmation. A form has no such problem: the person typed
    the values. What they still cannot do is write a record they would not be able to see, and
    they cannot save over a version of a record that has moved on since they read it.
    """

    def setUp(self):
        super().setUp()
        self.add_product()
        self.add_records()
        self.headers = {"Authorization": f"Bearer {create_token('demo-admin', TENANT)}"}

    def create(self, entity: str, **fields):
        return self.client.post(f"/api/products/{PRODUCT}/records/{entity}",
                                headers=self.headers, json={"fields": fields})

    def edit(self, entity: str, record_id: str, revision: int, **changes):
        return self.client.put(f"/api/products/{PRODUCT}/records/{entity}/{record_id}",
                               headers=self.headers, json={"changes": changes, "revision": revision})

    def read(self, entity: str, record_id: str) -> dict:
        records = self.client.get(f"/api/products/{PRODUCT}/records", headers=self.headers)
        self.assertEqual(records.status_code, 200, records.text)
        found = [row for row in records.json()["records"][entity] if row["id"] == record_id]
        self.assertEqual(len(found), 1, found)
        return found[0]

    def test_a_record_can_be_created_from_a_form_and_read_back(self):
        created = self.create("book", title="Star Atlas", status="On shelf", keeper=self.rosa.id)
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual(body["title"], "Star Atlas")
        self.assertEqual(body["revision"], 1)
        self.assertEqual(self.read("book", body["id"])["title"], "Star Atlas")

    def test_a_form_creates_without_any_execution_key(self):
        """The point of the form path: nothing proposed this, so nothing needs to be bound."""
        created = self.create("book", title="Rope Work", status="On shelf", keeper=self.otto.id)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertNotIn("outcome", created.json())

    def test_the_definition_decides_what_a_form_may_send(self):
        self.assertEqual(self.create("book", title="No Keeper", status="On shelf").status_code, 422)
        self.assertEqual(self.create("book", title="Odd", status="Lost",
                                     keeper=self.rosa.id).status_code, 422)
        self.assertEqual(self.create("book", title="Extra", status="On shelf",
                                     keeper=self.rosa.id, shelf="A4").status_code, 422)
        self.assertEqual(self.create("nothing", title="Nowhere").status_code, 404)

    def test_a_reference_to_nothing_is_refused(self):
        refused = self.create("book", title="Ghost", status="On shelf", keeper="nobody")
        self.assertEqual(refused.status_code, 409, refused.text)

    def test_an_edit_says_which_version_it_was_made_against(self):
        current = self.read("book", self.tide.id)
        changed = self.edit("book", self.tide.id, current["revision"], status="On loan")
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["status"], "On loan")
        self.assertEqual(changed.json()["revision"], current["revision"] + 1)

    def test_an_edit_against_a_version_that_moved_is_refused_and_shows_what_it_is_now(self):
        stale = self.read("book", self.tide.id)["revision"]
        self.store.update(self.definition, "book", self.tide.id, {"status": "On loan"})
        refused = self.edit("book", self.tide.id, stale, status="On shelf")
        self.assertEqual(refused.status_code, 409, refused.text)
        detail = refused.json()["detail"]
        self.assertEqual(detail["record"]["status"], "On loan")
        self.assertGreater(detail["record"]["revision"], stale)
        self.assertEqual(self.read("book", self.tide.id)["status"], "On loan")

    def test_a_field_the_definition_holds_fixed_cannot_be_edited(self):
        current = self.read("librarian", self.rosa.id)
        refused = self.edit("librarian", self.rosa.id, current["revision"], name="Someone Else")
        self.assertEqual(refused.status_code, 422, refused.text)

    def test_a_record_outside_somebody_reach_is_neither_edited_nor_revealed(self):
        """Another organization's member gets the same answer as for a product that is not there."""
        self.directory.create_organization("somebody-else", "Somebody Else")
        self.directory.add_member("somebody-else", "their-admin", "org_admin")
        headers = {"Authorization": f"Bearer {create_token('their-admin', 'somebody-else')}"}
        refused = self.client.put(f"/api/products/{PRODUCT}/records/book/{self.tide.id}",
                                  headers=headers, json={"changes": {"status": "On loan"},
                                                         "revision": 1})
        self.assertEqual(refused.status_code, 404, refused.text)
        self.assertEqual(self.read("book", self.tide.id)["status"], "On shelf")

    def test_the_assistant_path_still_needs_its_action(self):
        keyed = self.client.post(f"/api/products/{PRODUCT}/records/book", headers={
            **self.headers, "X-Execution-Key": "not-a-key", "X-Session-Id": "s1",
        }, json={"fields": {"title": "Keyless", "status": "On shelf", "keeper": self.rosa.id}})
        self.assertEqual(keyed.status_code, 422, keyed.text)

    def test_a_form_edit_cannot_be_sent_down_the_assistant_path(self):
        """PUT is the person's own edit; PATCH stays the assistant's, with the key that bound it."""
        current = self.read("book", self.tide.id)
        patched = self.client.patch(
            f"/api/products/{PRODUCT}/records/book/{self.tide.id}", headers=self.headers,
            json={"action": "update_book", "changes": {"status": "On loan"}})
        self.assertEqual(patched.status_code, 422, patched.text)
        self.assertEqual(self.read("book", self.tide.id)["revision"], current["revision"])


class TheScreenAndTheAssistantAgreeTest(AddedProductFixture):
    """Both read a product the same way, whichever way that product keeps its records.

    A product that brought its own record source and one that is only a definition are read
    through the same seam. Reading the store directly was right for the second and silently
    wrong for the first: its screens showed nothing while its assistant answered about records
    that were plainly there.
    """

    def setUp(self):
        super().setUp()
        self.headers = {"Authorization": f"Bearer {create_token('demo-admin', TENANT)}"}

    def records(self, product: str, scope: str | None = None) -> dict:
        query = f"?workspace_scope_id={scope}" if scope else ""
        response = self.client.get(f"/api/products/{product}/records{query}", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["records"]

    def test_a_product_that_brought_its_own_records_shows_them(self):
        """The shipped product keeps its records elsewhere. Its screens still show them."""
        held = self.records(DEMO, DEMO_SCOPE)
        self.assertTrue(any(rows for rows in held.values()),
                        f"the shipped product read as empty: {held}")

    def test_what_the_screen_shows_is_what_the_assistant_counts(self):
        spoken = self.ask("how many tickets are there", product=DEMO, scope=DEMO_SCOPE)["speech"]
        shown = self.records(DEMO, DEMO_SCOPE)
        counted = max((len(rows) for rows in shown.values()), default=0)
        self.assertTrue(any(str(len(rows)) in spoken for rows in shown.values() if rows),
                        f"the assistant said {spoken!r}, the screen holds {counted}")

    def test_a_product_that_is_only_a_definition_is_read_the_same_way(self):
        self.add_product()
        self.add_records()
        held = self.records(PRODUCT)
        self.assertEqual({row["id"] for row in held["book"]}, {self.tide.id, self.charts.id})
        self.assertEqual({row["id"] for row in held["librarian"]}, {self.rosa.id, self.otto.id})

    def test_neither_product_can_see_the_other(self):
        self.add_product()
        self.add_records()
        self.assertNotIn("book", self.records(DEMO, DEMO_SCOPE))
        added = self.records(PRODUCT)
        self.assertNotIn("issue", added)
        self.assertFalse(any("Tide Tables" in str(rows) for entity, rows in
                             self.records(DEMO, DEMO_SCOPE).items()))

    def test_a_record_carries_the_version_an_edit_is_made_against(self):
        self.add_product()
        self.add_records()
        for row in self.records(PRODUCT)["book"]:
            self.assertGreaterEqual(row["revision"], 1)


if __name__ == "__main__":
    unittest.main()
