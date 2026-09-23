"""Describing a product and getting one that is actually about what was described.

Somebody adding a product to Pixel says what it is made of - deals, contacts, sales reps - and
Pixel writes the definition. The test that matters is not that a definition appears, but that the
product which appears is theirs: its words, its records, its people, and nothing borrowed from a
product this repository happens to ship.
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

from pydantic import ValidationError  # noqa: E402

from app.auth import create_token  # noqa: E402
from app.definitions.authoring import store_definition  # noqa: E402
from app.definitions.drafting import ProductDraft, draft_definition, draft_text  # noqa: E402
from app.definitions.loader import parse_definition  # noqa: E402
from app.definitions.organizations import OrganizationDirectory  # noqa: E402
from app.definitions.registry import DefinitionRegistry  # noqa: E402
from app.record_access import grant_records  # noqa: E402
from app.services.record_store import PRIMARY, RecordStore  # noqa: E402

TENANT, PRODUCT, DEFINITION = "pixel-dev", "acme-crm", "acme_crm"

CRM = {
    "product_name": "Acme CRM", "definition_id": DEFINITION, "assistant_name": "Edith",
    "things": [
        {"name": "deal", "label": "Deal", "plural": "Deals", "fields": [
            {"name": "title", "type": "text", "required": True},
            {"name": "stage", "type": "enum", "values": ["New", "Qualified", "Won", "Lost"]},
            {"name": "value", "type": "integer"}]},
        {"name": "contact", "label": "Contact", "plural": "Contacts", "fields": [
            {"name": "full_name", "type": "text", "required": True}]},
        {"name": "rep", "label": "Sales rep", "plural": "Sales reps", "people": True, "fields": [
            {"name": "name", "type": "text", "required": True},
            {"name": "region", "type": "text"}]},
    ],
}


class DraftingTest(unittest.TestCase):
    def draft(self, **changes) -> dict:
        return draft_definition(ProductDraft.model_validate({**CRM, **changes}))

    def test_a_description_becomes_a_definition_this_platform_accepts(self):
        parsed = parse_definition(draft_text(ProductDraft.model_validate(CRM)).encode())
        self.assertEqual(parsed.identity.product_name, "Acme CRM")
        self.assertEqual(sorted(parsed.entities), ["contact", "deal", "rep"])

    def test_the_product_is_about_what_was_described(self):
        text = draft_text(ProductDraft.model_validate(CRM)).lower()
        for theirs in ("deal", "contact", "sales rep", "stage", "qualified"):
            self.assertIn(theirs, text, theirs)
        for borrowed in ("book", "librarian", "ticket", "cycle", "sprint"):
            self.assertNotIn(borrowed, text, f"{borrowed} belongs to another product")

    def test_identifiers_are_ones_a_person_can_read_out(self):
        entities = self.draft()["entities"]
        self.assertEqual(entities["deal"]["id"], {"strategy": "prefix", "prefix": "DEAL"})
        self.assertEqual(entities["rep"]["id"]["strategy"], "slug", "people are named, not numbered")

    def test_initials_are_used_when_a_name_has_several_words(self):
        drafted = self.draft(things=[{"name": "support_case", "label": "Support case",
                                      "plural": "Support cases",
                                      "fields": [{"name": "title", "type": "text", "required": True}]}])
        self.assertEqual(drafted["entities"]["support_case"]["id"]["prefix"], "SC")

    def test_two_things_never_share_an_identifier_prefix(self):
        drafted = self.draft(things=[
            {"name": "deal", "label": "Deal", "plural": "Deals",
             "fields": [{"name": "title", "type": "text", "required": True}]},
            {"name": "dealer", "label": "Dealer", "plural": "Dealers",
             "fields": [{"name": "title", "type": "text", "required": True}]},
        ])
        prefixes = [entity["id"]["prefix"] for entity in drafted["entities"].values()]
        self.assertEqual(len(set(prefixes)), 2, prefixes)

    def test_no_scopes_are_invented(self):
        """Who may see what is never guessed: guessing it would widen somebody's access."""
        drafted = self.draft()
        self.assertEqual(drafted["scope"]["anchor"], "deal")
        self.assertNotIn("record_scopes", drafted)
        self.assertTrue(all(isinstance(path, list) for path in drafted["scope"]["paths"].values()))

    def test_a_create_does_not_demand_a_person_before_it_will_start(self):
        actions = self.draft()["actions"]
        self.assertNotIn("owner", actions["create_deal"]["fields"])
        self.assertIn("owner", actions["change_deal"]["fields"], "assigning is a change of owner")

    def test_a_name_that_may_not_be_changed_may_still_be_given(self):
        drafted = self.draft()
        self.assertFalse(drafted["entities"]["rep"]["fields"]["name"]["editable"])
        self.assertIn("name", drafted["actions"]["create_rep"]["fields"])

    def test_a_description_that_cannot_make_a_product_is_refused(self):
        for broken, why in (
            ({"things": []}, "nothing to keep"),
            ({"things": [{"name": "rep", "label": "Rep", "plural": "Reps", "people": True}]},
             "only people"),
            ({"things": [dict(CRM["things"][0]), dict(CRM["things"][0])]}, "the same thing twice"),
            ({"definition_id": "Not A Key"}, "an unusable identifier"),
            ({"things": [{"name": "deal", "label": "Deal", "plural": "Deals", "fields": [
                {"name": "stage", "type": "enum", "values": ["Only one"]}]}]}, "a choice of one"),
        ):
            with self.subTest(why=why), self.assertRaises(ValidationError):
                ProductDraft.model_validate({**CRM, **broken})

    def test_a_description_cannot_smuggle_anything_into_the_definition(self):
        for unsafe in ({"product_name": "Acme <script>alert(1)</script>"},
                       {"things": [{"name": "deal", "label": "../../etc/passwd", "plural": "Deals"}]},
                       {"things": [{"name": "deal", "label": "Deal", "plural": "Deals", "fields": [
                           {"name": "title", "type": "enum", "values": ["javascript:alert(1)", "b"]}]}]}):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValidationError):
                ProductDraft.model_validate({**CRM, **unsafe})


class TalkingToADraftedProductTest(EngineCutoverFixture):
    """The whole point: the product somebody described answers about their own work."""

    def setUp(self):
        super().setUp()
        # The authority is fixed when the application starts, so a test module that ran the
        # lifespan earlier can leave it pinned. Both are set here, so this suite states the
        # authority it tests under rather than inheriting whatever ran before it.
        for patcher in (unittest.mock.patch.dict(os.environ, {"PIXEL_ENGINE_MODE": "definition"}),
                        unittest.mock.patch.object(main, "_authority", "definition")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.turns: dict[str, int] = {}
        registry = DefinitionRegistry()
        store_definition(draft_text(ProductDraft.model_validate(CRM)),
                         definition_id=DEFINITION, version=1)
        registry.ensure_published(DEFINITION, 1)
        OrganizationDirectory(registry).bind_product(TENANT, PRODUCT, "planning-team", DEFINITION, 1)
        grant_records(TENANT, PRODUCT, "demo-admin", [], True)
        self.definition = registry.load(DEFINITION, 1).definition
        self.store = RecordStore(TENANT, PRODUCT, PRIMARY)
        self.mara = self.store.create(self.definition, "rep", {"name": "Mara Ellis", "region": "North"})
        self.theo = self.store.create(self.definition, "rep", {"name": "Theo Grant", "region": "South"})
        self.deals = [
            self.store.create(self.definition, "deal", {
                "title": title, "stage": stage, "value": value, "owner": owner})
            for title, stage, value, owner in (
                ("Northwind renewal", "Qualified", 48000, self.mara.id),
                ("Contoso expansion", "New", 12000, self.mara.id),
                ("Fabrikam pilot", "Won", 5000, self.theo.id))
        ]

    def ask(self, message: str, session: str = "crm") -> dict:
        self.turns[session] = self.turns.get(session, 0) + 1
        response = self.client.post("/api/turn", headers={
            "Authorization": f"Bearer {create_token('demo-admin', TENANT)}",
        }, json={"session_id": session, "turn_id": self.turns[session], "product_id": PRODUCT,
                 "message": message, "workspace_scope_id": PRIMARY})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def action(body: dict) -> str | None:
        return (body.get("validated_action") or {}).get("type")

    def test_api_draft_is_private_to_the_callers_organization_and_can_be_published(self):
        request = {**CRM, "definition_id": "drafted_crm"}
        drafted = self.client.post("/api/product-drafts", headers={
            "Authorization": f"Bearer {create_token('demo-admin', TENANT)}",
        }, json=request)
        self.assertEqual(drafted.status_code, 200, drafted.text)
        source = drafted.json()["definition"]
        # Ownership is stated by the definition's own identity block, not its product identity.
        identity = parse_definition(source.encode()).definition
        self.assertEqual(identity.ownership, "organization_private")
        self.assertEqual(identity.owner_organization, TENANT)

        published = self.client.post(f"/api/organizations/{TENANT}/products", headers={
            "Authorization": f"Bearer {create_token('demo-admin', TENANT)}",
        }, json={"product_id": "drafted-crm", "team_id": "planning-team",
                 "definition_id": "drafted_crm", "definition_version": 1,
                 "definition": source})
        self.assertEqual(published.status_code, 201, published.text)

    def test_it_lists_counts_and_filters_their_own_records(self):
        self.assertEqual(self.action(self.ask("show me the deals")), "OPEN_DEALS")
        counted = self.ask("how many deals are there")
        self.assertIn("Northwind renewal", counted["speech"])
        filtered = self.ask("deals for Mara")
        self.assertEqual(self.action(filtered), "DEALS_BY_OWNER")
        self.assertIn("Mara Ellis", filtered["speech"])

    def test_it_opens_changes_and_assigns_through_the_chat(self):
        opened = self.ask(f"open {self.deals[0].id}", session="work")
        self.assertEqual(self.action(opened), "OPEN_DEAL")
        # A change to somebody's records is confirmed before it is proposed for execution.
        asked = self.ask("move it to won", session="work")
        self.assertIsNone(self.action(asked))
        self.assertIn("Should I", asked["speech"])
        moved = self.ask("yes", session="work")
        self.assertEqual(self.action(moved), "CHANGE_DEAL")
        self.assertEqual(moved["validated_action"]["payload"]["stage"], "Won")
        self.ask("give it to Theo", session="work")
        assigned = self.ask("yes", session="work")
        self.assertEqual(self.action(assigned), "CHANGE_DEAL")
        self.assertEqual(assigned["validated_action"]["payload"]["owner"], self.theo.id)

    def test_it_creates_by_asking_for_what_it_needs(self):
        asked = self.ask("add a deal", session="new")
        self.assertIsNone(self.action(asked))
        self.assertIn("title", asked["speech"])
        made = self.ask("Adventure Works trial", session="new")
        self.assertEqual(self.action(made), "CREATE_DEAL")
        self.assertEqual(made["validated_action"]["payload"]["title"], "Adventure Works trial")

    def test_it_refuses_destruction_and_knows_nothing_of_other_products(self):
        self.assertEqual(self.ask("delete everything")["status"], "denied")
        stranger = self.ask("how many tickets are there", session="stranger")
        self.assertIsNone(self.action(stranger))
        self.assertNotIn("Northwind", stranger["speech"])

    def test_the_shipped_demo_is_untouched_beside_it(self):
        demo = self.client.post("/api/turn", headers={
            "Authorization": f"Bearer {create_token('demo-admin', TENANT)}",
        }, json={"session_id": "demo", "turn_id": 1, "product_id": "linear-demo",
                 "message": "how many tickets are there",
                 "workspace_scope_id": "workspace-product-eng"}).json()
        self.assertEqual(self.action(demo), "OPEN_ISSUES")
        self.assertNotIn("Northwind renewal", demo["speech"])


if __name__ == "__main__":
    unittest.main()
