from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from test_added_product import AddedProductFixture, TENANT, PRODUCT, DEFINITION
from app.auth import create_token
from app.engine.knowledge import KnowledgeContext
from app.product_knowledge import ApprovedKnowledge
from app import main


class ProductKnowledgeTest(AddedProductFixture):
    def setUp(self):
        super().setUp()
        authority = patch.object(main, "_authority", "definition")
        authority.start()
        self.addCleanup(authority.stop)
        self.add_product()

    def publish(self, **overrides):
        return self.client.post(f"/api/products/{PRODUCT}/knowledge", headers=self.headers, json={
            "title": "Loans", "text": "A book loan lasts fourteen days.", "approved": True, **overrides,
        })

    def context(self, version):
        binding = self.directory.product(TENANT, PRODUCT)
        return KnowledgeContext(tenant_id=TENANT, product_id=PRODUCT, definition_id=DEFINITION,
                                definition_version=1, definition_checksum=binding.definition_checksum,
                                knowledge_version=version, scope_label="primary")

    def test_publication_is_approved_and_version_pinned(self):
        self.assertEqual(self.publish(approved=False).status_code, 422)
        first = self.publish()
        self.assertEqual(first.status_code, 201, first.text)
        version = first.json()["version"]
        self.assertEqual(len(ApprovedKnowledge(self.context(version)).search("book loan")), 1)
        self.assertEqual(ApprovedKnowledge(self.context(version - 1)).search("book loan"), [])
        self.publish(title="Renewals", text="A loan renewal lasts seven days.")
        self.assertEqual(len(ApprovedKnowledge(self.context(version)).search("loan")), 1)
        self.assertEqual(len(ApprovedKnowledge(self.context(version + 1)).search("loan")), 2)

    def test_approved_source_reaches_the_generic_conversation(self):
        self.assertEqual(self.publish().status_code, 201)
        answer = self.ask("how long does a loan last")
        self.assertIn("fourteen days", answer["speech"])
        self.assertIsNone(answer["execution"])

    def test_added_product_voice_uses_its_definition_without_a_demo_config(self):
        with patch.object(main.speech_service, "synthesize", return_value=SimpleNamespace(
            engine="fake", voice="fake", audio=b"test", media_type="audio/mpeg",
        )) as synthesis:
            response = self.client.post("/api/speech", headers=self.headers, json={"product_id": PRODUCT, "text": "Hello"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(synthesis.call_args.kwargs["voice_style"], self.definition.identity.voice_style)

    def test_each_binding_dimension_prevents_cross_product_reads(self):
        version = self.publish().json()["version"]
        context = self.context(version)
        for change in ({"tenant_id": "another"}, {"product_id": "another"},
                       {"knowledge_version": 99}, {"definition_checksum": "f" * 64}):
            self.assertEqual(ApprovedKnowledge(replace(context, **change)).search("loan"), [])

    def test_member_cannot_publish_and_other_team_cannot_list(self):
        self.directory.add_member(TENANT, "reader", "team_member", "planning-team")
        headers = {"Authorization": "Bearer " + create_token("reader", TENANT)}
        denied = self.client.post(f"/api/products/{PRODUCT}/knowledge", headers=headers,
                                 json={"title": "Title", "text": "Content", "approved": True})
        self.assertEqual(denied.status_code, 403)
        self.directory.create_team(TENANT, "other", "Other")
        self.directory.add_member(TENANT, "outsider", "team_member", "other")
        headers = {"Authorization": "Bearer " + create_token("outsider", TENANT)}
        self.assertEqual(self.client.get(f"/api/products/{PRODUCT}/knowledge", headers=headers).status_code, 404)
        products = self.client.get(f"/api/organizations/{TENANT}/products", headers=headers)
        self.assertNotIn(PRODUCT, [p["product_id"] for p in products.json()["products"]])

    def test_the_document_a_question_is_about_outranks_one_that_merely_mentions_it(self):
        """A word every document uses cannot decide which of them is the answer.

        Ranking on how many distinct words two texts happen to share picks the longest document,
        not the relevant one, so a question about renewals gets answered about loans in general.
        """
        self.publish(title="Loans", text="A book loan lasts fourteen days for every member.")
        version = self.publish(title="Renewals",
                               text="A loan renewal lasts seven days.").json()["version"]
        found = ApprovedKnowledge(self.context(version)).search("how long is a renewal")
        self.assertEqual(found[0].title, "Renewals")
        self.assertTrue(found[0].grounds_answer)

    def test_a_question_reaches_the_document_whose_title_it_echoes(self):
        """Both titles name the subject, so the words "what" and "how" have to decide.

        "What is Pixel?" was answered from "How Pixel works" because the two tied on "Pixel" and
        the tie went to whichever document sorted first.
        """
        # The document about how it works sorts first, as Pixel's own "how-pixel-works" does.
        ids = [SimpleNamespace(hex="0" * 32), SimpleNamespace(hex="f" * 32)]
        with patch("app.product_knowledge.uuid4", side_effect=ids):
            self.publish(title="How Ledger works", text="Ledger checks every entry twice before saving.")
            version = self.publish(title="What Ledger is",
                                   text="Ledger is a book of accounts for small teams.").json()["version"]
        knowledge = ApprovedKnowledge(self.context(version))
        self.assertEqual(knowledge.search("What is Ledger?")[0].title, "What Ledger is")
        self.assertEqual(knowledge.search("what does ledger do")[0].title, "What Ledger is")
        self.assertEqual(knowledge.search("How does Ledger work?")[0].title, "How Ledger works")

    def test_one_word_in_common_is_not_an_answer(self):
        version = self.publish(title="Loans",
                               text="A book loan lasts fourteen days.").json()["version"]
        found = ApprovedKnowledge(self.context(version)).search("where do I park my car")
        self.assertFalse([passage for passage in found if passage.grounds_answer])

    def test_empty_text_is_rejected_and_capacity_is_bounded(self):
        self.assertEqual(self.publish(text=" ").status_code, 422)
        for _ in range(4):
            self.assertEqual(self.publish(text="x" * 32000).status_code, 201)
        self.assertEqual(self.publish().status_code, 409)
