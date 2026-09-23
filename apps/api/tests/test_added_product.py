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

from app.auth import create_token  # noqa: E402
from app.definitions.authoring import store_definition  # noqa: E402
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
        authority = unittest.mock.patch.dict(os.environ, {"PIXEL_ENGINE_MODE": "definition"})
        authority.start()
        self.addCleanup(authority.stop)
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
            session: str = "added") -> dict:
        """One turn of a conversation. Messages sharing a session continue it, as a visitor's do."""
        self.turns[session] = self.turns.get(session, 0) + 1
        response = self.client.post("/api/turn", headers={
            "Authorization": f"Bearer {create_token('demo-admin', TENANT)}",
        }, json={
            "session_id": session, "turn_id": self.turns[session], "product_id": product,
            "message": message, "workspace_scope_id": scope,
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


if __name__ == "__main__":
    unittest.main()
