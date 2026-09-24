"""Milestone 3.2 slice 4b: the response composer and platform conversation intents.

The rule under test is the one that protects a visitor from being told something untrue: **the
wording follows the lifecycle state.** A proposal is described as a proposal; only a committed
write is described as done. Model-written sentences are replaced in every stage in this slice.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine.actions import FilterParam, GenericAction, RecordRef
from app.engine.composer import (
    COMPLETION_CLAIMS,
    MAX_MODEL_SPEECH,
    PLATFORM_KNOWLEDGE_UNAVAILABLE,
    STAGE_TEMPLATES,
    TemplateNotAllowed,
    MissingTemplate,
    ResponseComposer,
    Stage,
    describe_changes,
)
from app.engine.conversation import (
    KNOWLEDGE_UNAVAILABLE_KEY,
    CapabilityPolicy,
    Conversational,
    capability_sentence,
    detect,
    offerable,
)
from app.engine.knowledge import (
    answerable,
    Grounding,
    KnowledgeLookup,
    KnowledgePassage,
    NoKnowledge,
    ground,
)
from app.engine.lookup import RecordView
from app.engine.normalizer import Normalizer
from app.engine.snapshot import TurnSnapshot
from engine_fixtures import SampleDesk, engine_definition, load_engine_definition


# Every action is translatable and permitted: used where the test is about something else.
ALLOW_ALL = CapabilityPolicy(translatable=lambda key: True, permitted=lambda key: True)


def desk_snapshot(entities=("contact", "agent")) -> TurnSnapshot:
    desk = SampleDesk()
    records: dict[str, tuple[RecordView, ...]] = {}
    if "contact" in entities:
        records["contact"] = tuple(r for r, account in desk.contacts.values() if account == "ACC-1")
    if "agent" in entities:
        records["agent"] = tuple(
            RecordView("agent", agent_id, name, {})
            for agent_id, (name, account) in desk.agents.items() if account == "ACC-1"
        )
    if "account" in entities:
        records["account"] = (desk.accounts["ACC-1"],)
    return TurnSnapshot(records=records, scope_label="ACC-1", definition_checksum="sample",
                        taken_at=1.0, people_entity="agent", person_fields={"contact": "owner"})


class ComposerFixture(unittest.TestCase):
    def setUp(self):
        self.definition = load_engine_definition(document=engine_definition())
        self.composer = ResponseComposer(self.definition)
        self.update = GenericAction.for_definition(
            self.definition, "update_contact", target=RecordRef("contact", "CON-1"),
            fields={"status": "Closed"},
        )
        self.open_view = GenericAction.for_definition(
            self.definition, "open_contacts", view="contacts"
        )


class LifecycleWordingTest(ComposerFixture):
    def test_a_proposal_is_described_as_a_proposal(self):
        reply = self.composer.proposed(self.update)
        self.assertEqual(reply.stage, Stage.PROPOSED)
        self.assertEqual(reply.template_key, "record_update_proposed")
        self.assertIn("CON-1", reply.speech)
        self.assertNotIn("is now", reply.speech.lower())

    def test_a_confirmation_question_names_the_exact_record(self):
        reply = self.composer.awaiting_confirmation(self.update)
        self.assertEqual(reply.stage, Stage.AWAITING_CONFIRMATION)
        self.assertEqual(reply.template_key, "confirm_action")
        self.assertIn("CON-1", reply.speech)
        self.assertTrue(reply.speech.strip().endswith("?"), reply.speech)

    def test_only_a_committed_write_is_described_as_done(self):
        proposed = self.composer.proposed(self.update)
        executed = self.composer.executed(self.update)
        self.assertNotEqual(proposed.template_key, executed.template_key)
        self.assertEqual(executed.template_key, "record_updated")
        self.assertEqual(executed.stage, Stage.EXECUTED)

    def test_the_same_action_words_differently_at_each_stage(self):
        stages = {
            self.composer.proposed(self.update).speech,
            self.composer.awaiting_confirmation(self.update).speech,
            self.composer.executed(self.update).speech,
            self.composer.cancelled().speech,
        }
        self.assertEqual(len(stages), 4, "each lifecycle state must read differently")

    def test_a_cancelled_action_says_nothing_changed(self):
        reply = self.composer.cancelled()
        self.assertEqual(reply.stage, Stage.CANCELLED)
        self.assertEqual(reply.template_key, "action_cancelled")

    def test_a_failure_never_reads_as_success(self):
        reply = self.composer.failed(self.update, "fallback")
        self.assertEqual(reply.stage, Stage.FAILED)
        for claim in ("is now", "i updated", "done"):
            self.assertNotIn(claim, reply.speech.lower())

    def test_a_creation_and_an_update_use_different_templates(self):
        create = GenericAction.for_definition(
            self.definition, "create_contact", fields={"name": "Zed", "account": "ACC-1"}
        )
        self.assertEqual(self.composer.proposed(create).template_key, "record_create_proposed")
        self.assertEqual(self.composer.executed(create, record_id="CON-9").template_key,
                         "record_created")

    def test_every_capability_has_wording_for_proposal_and_completion(self):
        actions = {
            "open_contacts": {"view": "contacts"},
            "open_contact": {"target": RecordRef("contact", "CON-1")},
            "contacts_by_owner": {"filter": FilterParam("owner", "ana-lopez")},
            "create_contact": {"fields": {"name": "Zed", "account": "ACC-1"}},
            "update_contact": {"target": RecordRef("contact", "CON-1"), "fields": {"status": "Closed"}},
            "highlight_mail": {"view": "channels", "control": "mail_card"},
        }
        for action_key, params in actions.items():
            with self.subTest(action_key):
                action = GenericAction.for_definition(self.definition, action_key, **params)
                values = {"person": "Ana Lopez", "count": "2", "records": "CON-1",
                          "record_id": "CON-1",
                          "record_title": "Dana Reyes", "field": "owner", "value": "Ana Lopez",
                          "scope": "ACC-1"}
                self.assertTrue(self.composer.proposed(action, **values).speech)
                self.assertTrue(self.composer.executed(action, **values).speech)

    def test_the_exact_change_is_spelled_out(self):
        self.assertEqual(describe_changes({"status": "Closed", "owner": "ana-lopez"}),
                         "owner to ana-lopez, status to Closed")

    def test_platform_identity_does_not_need_a_product_template(self):
        document = engine_definition()
        document["responses"].pop("greeting_named")
        composer = ResponseComposer(load_engine_definition(document=document))
        reply = composer.answer("greeting_named", visitor="Priya")
        self.assertIn('Priya', reply.speech)
        self.assertFalse(reply.product_copy)

    def test_a_placeholder_with_no_value_is_an_error_not_an_empty_gap(self):
        with self.assertRaises(MissingTemplate):
            self.composer.answer("greeting_named")  # needs a visitor name, and none was given


class ModelSpeechTest(ComposerFixture):
    def test_even_a_harmless_model_sentence_is_replaced_for_now(self):
        """Nothing in this slice can tie a sentence to what actually happened."""
        reply = self.composer.from_model(
            "Cycles are time-boxed planning periods.", Stage.ANSWER, "knowledge_unavailable",
        )
        self.assertFalse(reply.from_model)
        self.assertTrue(reply.replaced_model_speech)

    def test_a_completion_claim_about_a_proposal_is_replaced_not_edited(self):
        reply = self.composer.from_model(
            "Done! I've closed CON-1 for you.", Stage.PROPOSED, "record_update_proposed",
            record_id="CON-1", changes="status to Closed",
        )
        self.assertTrue(reply.replaced_model_speech)
        self.assertFalse(reply.from_model)
        self.assertEqual(reply.template_key, "record_update_proposed")
        self.assertNotIn("Done", reply.speech)

    def test_every_completion_phrase_is_caught_before_execution(self):
        for claim in COMPLETION_CLAIMS:
            with self.subTest(claim=claim):
                reply = self.composer.from_model(
                    f"Sure, {claim} the contact.", Stage.AWAITING_CONFIRMATION, "confirm_action",
                    record_id="CON-1", changes="status to Closed",
                )
                self.assertTrue(reply.replaced_model_speech, claim)

    def test_even_after_a_write_the_wording_comes_from_the_committed_result(self):
        """The write proves one operation happened; it does not license any other sentence."""
        reply = self.composer.from_model(
            "Done, CON-1 is now closed.", Stage.EXECUTED, "record_updated",
            record_id="CON-1", changes="status to Closed",
        )
        self.assertTrue(reply.replaced_model_speech)
        self.assertEqual(reply.template_key, "record_updated")

    def test_model_speech_that_is_not_plain_text_is_replaced(self):
        reply = self.composer.from_model(
            "<b>I'll close it</b>", Stage.PROPOSED, "record_update_proposed",
            record_id="CON-1", changes="status to Closed",
        )
        self.assertTrue(reply.replaced_model_speech)

    def test_empty_model_speech_is_replaced(self):
        reply = self.composer.from_model(
            "   ", Stage.PROPOSED, "record_update_proposed",
            record_id="CON-1", changes="status to Closed",
        )
        self.assertTrue(reply.replaced_model_speech)


class ConversationalTurnTest(unittest.TestCase):
    def setUp(self):
        self.definition = load_engine_definition(document=engine_definition())
        self.normalizer = Normalizer(self.definition.vocabulary)

    def detect(self, message: str, **kwargs):
        return detect(self.normalizer.normalize(message), **kwargs)

    def test_a_greeting_is_recognised_by_the_platform(self):
        turn = self.detect("hello")
        self.assertEqual(turn.kind, Conversational.GREETING)
        self.assertEqual(turn.template_key, "greeting")

    def test_a_known_visitor_gets_the_named_greeting(self):
        turn = self.detect("hi", visitor_name="Priya")
        self.assertEqual(turn.kind, Conversational.GREETING_NAMED)
        self.assertEqual(turn.visitor_name, "Priya")

    def test_an_introduction_is_a_named_greeting(self):
        turn = self.detect("I'm Priya")
        self.assertEqual(turn.kind, Conversational.GREETING_NAMED)
        self.assertEqual(turn.visitor_name, "Priya")

    def test_an_introduction_shaped_sentence_that_is_not_one_is_ignored(self):
        self.assertIsNone(self.detect("I'm looking for the issue list"))

    def test_identity_questions_are_recognised(self):
        for message in ("who are you", "what is your name", "are you a bot"):
            with self.subTest(message):
                self.assertEqual(self.detect(message).kind, Conversational.IDENTITY)

    def test_capability_questions_are_recognised(self):
        for message in ("what can you do", "how can you help"):
            with self.subTest(message):
                self.assertEqual(self.detect(message).kind, Conversational.CAPABILITIES)

    def test_a_product_request_is_not_a_conversational_turn(self):
        for message in ("show me the contacts", "close CON-1", "who owns CON-1"):
            with self.subTest(message):
                self.assertIsNone(self.detect(message))

    def test_the_words_come_from_the_definition_not_from_core(self):
        composer = ResponseComposer(self.definition, visitor_name="Priya")
        turn = self.detect("hi", visitor_name="Priya")
        reply = composer.answer(turn.template_key)
        self.assertIn(self.definition.identity.product_name, reply.speech)
        self.assertIn("Priya", reply.speech)


class OfferableActionTest(unittest.TestCase):
    def setUp(self):
        self.definition = load_engine_definition(document=engine_definition())

    def test_an_action_the_adapter_cannot_express_is_never_offered(self):
        """The `create_member` case: declared by the definition, untranslatable today."""
        offers = offerable(self.definition, desk_snapshot(),
                           CapabilityPolicy(translatable=lambda key: key != "update_contact",
                                            permitted=lambda key: True))
        self.assertNotIn("update_contact", offers.keys)
        self.assertIn("open_contacts", offers.keys)

    def test_an_action_the_caller_may_not_use_is_never_offered(self):
        offers = offerable(self.definition, desk_snapshot(),
                           CapabilityPolicy(translatable=lambda key: True,
                                            permitted=lambda key: not key.startswith("create")))
        self.assertNotIn("create_contact", offers.keys)

    def test_an_action_over_an_entity_with_nothing_visible_is_not_offered(self):
        empty = TurnSnapshot(records={"contact": (), "agent": ()}, scope_label="ACC-1",
                             definition_checksum="sample", taken_at=1.0)
        offers = offerable(self.definition, empty, ALLOW_ALL)
        self.assertNotIn("open_contact", offers.keys, "nothing to open")
        self.assertIn("create_contact", offers.keys, "creating is still possible")

    def test_everything_offered_is_something_the_definition_declares(self):
        offers = offerable(self.definition, desk_snapshot(), ALLOW_ALL)
        self.assertTrue(offers.keys)
        for key in offers.keys:
            self.assertIn(key, self.definition.actions)

    def test_the_capability_sentence_reads_as_a_list_of_real_abilities(self):
        offers = offerable(self.definition, desk_snapshot(), ALLOW_ALL)
        sentence = capability_sentence(offers, self.definition)
        self.assertIn(" and ", sentence)
        self.assertNotIn("..", sentence)

    def test_an_empty_offer_list_produces_no_sentence(self):
        empty = TurnSnapshot(records={}, scope_label="ACC-1", definition_checksum="x", taken_at=1.0)
        offers = offerable(self.definition, empty, CapabilityPolicy.nothing())
        self.assertTrue(offers.is_empty)
        self.assertEqual(capability_sentence(offers, self.definition), "")


class KnowledgeFallbackTest(unittest.TestCase):
    """Knowledge availability is asserted by the platform (the reopened 4b boundary).

    Slice 4b first let the product word this reply. The stakeholder review reversed that: whether
    approved knowledge exists is a platform fact, so the product contributes only its name.
    """

    def test_with_no_knowledge_source_the_assistant_says_so_in_platform_words(self):
        definition = load_engine_definition(document=engine_definition())
        reply = ResponseComposer(definition).answer(KNOWLEDGE_UNAVAILABLE_KEY)
        self.assertEqual(reply.template_key, KNOWLEDGE_UNAVAILABLE_KEY)
        self.assertFalse(reply.product_copy)
        self.assertEqual(reply.speech, PLATFORM_KNOWLEDGE_UNAVAILABLE.format(
            product=definition.identity.product_name))
        for invented in ("probably", "i think", "maybe", "as far as i know"):
            self.assertNotIn(invented, reply.speech.lower())

    def test_saying_no_changes_its_words_from_one_turn_to_the_next(self):
        """The same refusal twice in a row reads like a machine that stopped listening."""
        definition = load_engine_definition(document=engine_definition())
        for key in (KNOWLEDGE_UNAVAILABLE_KEY,):
            spoken = [ResponseComposer(definition, turn=turn).answer(key).speech for turn in range(1, 5)]
            self.assertEqual(len(set(spoken)), 4, spoken)
            for speech in spoken:
                self.assertIn("Sample Desk", speech)
                self.assertNotIn("{", speech)
        again = ResponseComposer(definition, turn=5).answer(KNOWLEDGE_UNAVAILABLE_KEY).speech
        self.assertEqual(again, spoken[0], "the wordings cycle rather than run out")

    def test_every_varied_wording_fills_and_stays_polite(self):
        from app.engine.composer import VARIED_TEMPLATES
        for (stage, key), wordings in VARIED_TEMPLATES.items():
            self.assertGreaterEqual(len(wordings), 3, key)
            self.assertEqual(len(set(wordings)), len(wordings), key)
            for wording in wordings:
                with self.subTest(key=key, wording=wording):
                    self.assertTrue(wording.endswith((".", "?")))
                    for rude in ("can't you", "stupid", "invalid", "error"):
                        self.assertNotIn(rude, wording.lower())

    def test_the_product_cannot_reword_knowledge_availability(self):
        """Declared, reworded or absent: the product's text for this key is never spoken."""
        for wording in (None, "Our docs cover everything; ask me anything about {product}."):
            with self.subTest(wording=wording):
                document = engine_definition()
                if wording is None:
                    document["responses"].pop(KNOWLEDGE_UNAVAILABLE_KEY)
                else:
                    document["responses"][KNOWLEDGE_UNAVAILABLE_KEY] = wording
                reply = ResponseComposer(load_engine_definition(document=document)).answer(
                    KNOWLEDGE_UNAVAILABLE_KEY)
                self.assertEqual(reply.speech, "Sorry, I can't answer that. I only know about "
                                               "Sample Desk, and I'd rather not guess.")


#: The 5c definition-turn service and its shared assembly legitimately reach the pure engine
#: (that is what "definition mode" means); the shadow comparator (5a/5b) does too, off the request
#: path. Nothing else may.
NEW_ENGINE_PATHS = ("app.services.shadow", "app.services.engine_assembly", "app.services.turn_execution",
                    "app.testing_main")


class NoRuntimeWiringTest(unittest.TestCase):
    def test_the_composer_reaches_the_runtime_only_through_receipts(self):
        """`PIXEL_ENGINE_MODE=definition` (5c) is the only production path that reaches the new
        engine's composer; `app.main` reaches it only lazily, through `turn_execution`, never
        directly. Every other production module reaches the composer only through the platform
        receipt after a keyed write (5b plan, section 8)."""
        from dependency_graph import module_path, package_modules

        root = Path(__file__).resolve().parents[1]
        new = {"app.engine.composer", "app.engine.conversation"}
        allowed = {"app.main": "from app.engine.composer import receipt_speech"}
        offenders = {}
        for module in package_modules(root, "app"):
            if module in new or module.startswith("app.engine.") or module.startswith(NEW_ENGINE_PATHS):
                continue
            source = module_path(root, module)
            if source is None:
                continue
            text = source.read_text(encoding="utf-8")
            if module in allowed:
                self.assertIn(allowed[module], text)
                text = text.replace(allowed[module], "")
            hits = sorted(name for name in new if name in text)
            if hits:
                offenders[module] = hits
        self.assertEqual(offenders, {}, "only the receipt entry point may be wired before 5c")


class KnowledgeBoundaryTest(unittest.TestCase):
    """The knowledge seam: read-only, scope-bound, and honest when nothing is installed."""

    def setUp(self):
        self.definition = load_engine_definition(document=engine_definition())
        self.composer = ResponseComposer(self.definition)

    def test_the_default_is_no_knowledge_at_all(self):
        """A deployment with nothing installed cannot answer from a stale or foreign source."""
        self.assertIsInstance(NoKnowledge(), KnowledgeLookup)
        self.assertEqual(NoKnowledge().search("how do cycles work", 2), [])
        self.assertFalse(ground(NoKnowledge(), "how do cycles work").is_grounded)

    def test_search_takes_no_scope_product_or_tenant(self):
        """Scope is fixed when the lookup is built, exactly as it is for records."""
        import inspect

        parameters = set(inspect.signature(NoKnowledge.search).parameters)
        self.assertEqual(parameters, {"self", "text", "limit"})

    def test_an_ungrounded_question_is_answered_in_the_products_words(self):
        reply = self.composer.knowledge_answer(ground(NoKnowledge(), "what is a cycle"))
        self.assertEqual(reply.stage, Stage.UNGROUNDED)
        self.assertEqual(reply.template_key, KNOWLEDGE_UNAVAILABLE_KEY)
        self.assertIn(self.definition.identity.product_name, reply.speech)

    def test_an_ungrounded_question_is_not_a_refusal(self):
        """The request was fine; the deployment simply has nothing to answer from."""
        reply = self.composer.knowledge_answer(Grounding())
        self.assertNotEqual(reply.stage, Stage.REFUSED)

    def test_a_grounded_answer_comes_from_the_passages(self):
        passage = KnowledgePassage("Cycles", "docs/product/cycles.md", "Cycles are time-boxed.")
        reply = self.composer.knowledge_answer(Grounding((passage,)))
        self.assertEqual(reply.stage, Stage.ANSWER)
        self.assertEqual(reply.speech, "From the product documentation: Cycles are time-boxed.")
        self.assertEqual(reply.sources, ("docs/product/cycles.md",))

    def test_a_knowledge_answer_speaks_the_passage_not_a_model_sentence(self):
        """Retrieval is not grounding: a sentence beside a passage can say anything."""
        passage = KnowledgePassage("Cycles", "docs/product/cycles.md", "Cycles are time-boxed.")
        reply = self.composer.knowledge_answer(Grounding((passage,)))
        self.assertEqual(reply.speech, "From the product documentation: Cycles are time-boxed.")
        self.assertFalse(reply.from_model)

    def test_sources_are_listed_once_each(self):
        passages = (
            KnowledgePassage("A", "docs/product/cycles.md", "one"),
            KnowledgePassage("B", "docs/product/cycles.md", "two"),
            KnowledgePassage("C", "docs/product/issues.md", "three"),
        )
        self.assertEqual(Grounding(passages).sources,
                         ("docs/product/cycles.md", "docs/product/issues.md"))

    def test_an_empty_question_finds_nothing(self):
        class Everything:
            def search(self, text, limit):
                return [KnowledgePassage("t", "s", "snippet")]

        self.assertFalse(ground(Everything(), "   ").is_grounded)

    def test_the_passage_limit_is_honoured_even_if_a_source_ignores_it(self):
        class TooMany:
            def search(self, text, limit):
                return [KnowledgePassage(f"t{i}", f"s{i}", "x") for i in range(10)]

        self.assertEqual(len(ground(TooMany(), "anything", limit=2).passages), 2)

    def test_only_a_real_match_is_quoted_as_an_answer(self):
        """A weak match may support an action reply, but is never spoken as the answer."""
        class Mixed:
            def search(self, text, limit):
                return [KnowledgePassage("Weak", "weak.md", "one", grounds_answer=False),
                        KnowledgePassage("Strong", "strong.md", "two")]

        grounding = ground(Mixed(), "anything")
        self.assertEqual(len(grounding.passages), 2, "supporting passages are unchanged")
        self.assertEqual([p.source for p in answerable(grounding).passages], ["strong.md"])

    def test_a_passage_cannot_be_edited(self):
        passage = KnowledgePassage("Cycles", "docs/product/cycles.md", "Cycles are time-boxed.")
        with self.assertRaises(Exception):
            passage.snippet = "something else"

    def test_core_never_imports_a_products_retriever(self):
        """The seam is a protocol: core states the contract and imports no implementation."""
        source = (Path(__file__).resolve().parents[1] / "app/engine/knowledge.py").read_text(
            encoding="utf-8"
        )
        imports = [
            line.strip() for line in source.splitlines()
            if line.startswith(("import ", "from "))
        ]
        for line in imports:
            with self.subTest(line):
                self.assertNotIn("retriever", line.lower())
                self.assertNotIn("products", line)
                self.assertNotIn("app.services", line)


class SelfReviewDefectTest(ComposerFixture):
    """Defects found by probing slice 4b before calling it finished. One test each."""

    def test_a_proposal_never_uses_model_speech_at_all(self):
        """A phrase list can always be evaded, so the structure carries the guarantee.

        "The contact was closed" claims exactly as much as "done, I closed it", and no denylist
        catches every phrasing. Before a write commits, the product's template is the wording.
        """
        for speech in ("That is taken care of.", "The contact was closed.", "Closed.",
                       "Consider it handled.", "The status has changed.", "Sorted.",
                       "I'll close that contact."):
            with self.subTest(speech):
                reply = self.composer.from_model(
                    speech, Stage.PROPOSED, "record_update_proposed",
                    record_id="CON-1", changes="status to Closed",
                )
                self.assertTrue(reply.replaced_model_speech, speech)
                self.assertEqual(reply.template_key, "record_update_proposed")

    def test_no_stage_accepts_model_written_speech_in_this_slice(self):
        """Executing one action does not make any other sentence true."""
        for stage in (Stage.EXECUTED, Stage.ANSWER, Stage.PROPOSED):
            with self.subTest(stage):
                reply = self.composer.from_model(
                    "An entirely ordinary sentence.", stage, _allowed_key(stage),
                    record_id="CON-1", changes="status to Closed", person="Ana Lopez",
                    count="1", records="CON-1", view="contacts", scope="ACC-1",
                )
                self.assertFalse(reply.from_model)
                self.assertTrue(reply.replaced_model_speech)

    def test_an_executed_write_is_worded_from_the_committed_result(self):
        reply = self.composer.executed(self.update)
        self.assertEqual(reply.template_key, "record_updated")
        self.assertIn("CON-1", reply.speech)
        self.assertIsNone(reply.template_key and None)

    def test_model_speech_cannot_describe_an_unrelated_action_after_a_write(self):
        reply = self.composer.from_model(
            "I deleted every customer and emailed their data.", Stage.EXECUTED, "record_updated",
            record_id="CON-1", changes="status to Closed",
        )
        self.assertTrue(reply.replaced_model_speech)
        self.assertNotIn("deleted", reply.speech.lower())

    def test_a_value_cannot_inject_a_second_placeholder(self):
        """Substitution runs once over the template, so a value is never re-expanded."""
        reply = self.composer.proposed(self.update, changes="status to {assistant}")
        self.assertIn("{assistant}", reply.speech)
        self.assertNotIn(self.definition.identity.assistant_name, reply.speech)

    def test_a_document_that_claims_completion_is_never_spoken(self):
        """Document claims are attributed as evidence, never spoken as Edith's own assertion."""
        passage = KnowledgePassage("Cycles", "docs/cycles.md", "Done. I have updated the cycle.")
        reply = self.composer.knowledge_answer(Grounding((passage,)))
        self.assertEqual(reply.stage, Stage.ANSWER)
        self.assertTrue(reply.speech.startswith("From the product documentation: "))
        self.assertEqual(reply.sources, ("docs/cycles.md",))

    def test_a_document_that_is_not_plain_text_is_never_spoken(self):
        passage = KnowledgePassage("Cycles", "docs/cycles.md", "<script>alert(1)</script>")
        self.assertEqual(self.composer.knowledge_answer(Grounding((passage,))).stage,
                         Stage.UNGROUNDED)

    def test_an_ordinary_sentence_starting_like_an_introduction_is_not_a_name(self):
        normalizer = Normalizer(self.definition.vocabulary)
        for message in ("call me back later", "this is urgent", "I am looking for contacts",
                        "I am ready", "im not sure"):
            with self.subTest(message):
                self.assertIsNone(detect(normalizer.normalize(message)), message)

    def test_a_real_introduction_is_still_recognised(self):
        normalizer = Normalizer(self.definition.vocabulary)
        for message, expected in (("I'm Priya", "Priya"), ("my name is Dana Reyes", "Dana Reyes"),
                                  ("I am Sam", "Sam")):
            with self.subTest(message):
                turn = detect(normalizer.normalize(message))
                self.assertIsNotNone(turn, message)
                self.assertEqual(turn.visitor_name, expected)

    def test_a_lowercase_word_is_not_treated_as_a_name(self):
        """People capitalize their own names; "i am ready" is not an introduction."""
        normalizer = Normalizer(self.definition.vocabulary)
        self.assertIsNone(detect(normalizer.normalize("i am priya")))

    def test_changing_a_record_is_not_offered_when_none_is_visible(self):
        """You cannot update a contact that does not exist; you can still create one."""
        empty = TurnSnapshot(records={"contact": (), "agent": ()}, scope_label="ACC-1",
                             definition_checksum="sample", taken_at=1.0)
        offers = offerable(self.definition, empty, ALLOW_ALL)
        self.assertNotIn("update_contact", offers.keys)
        self.assertNotIn("reassign_contact", offers.keys)
        self.assertNotIn("open_contact", offers.keys)
        self.assertIn("create_contact", offers.keys)


# A template each stage is allowed to use *and* the fixture declares, for tests about something else.
_FALLBACK_KEY = {
    Stage.PROPOSED: "record_update_proposed",
    Stage.AWAITING_CONFIRMATION: "confirm_action",
    Stage.EXECUTED: "record_updated",
    Stage.CANCELLED: "action_cancelled",
    Stage.ANSWER: "knowledge_unavailable",
    Stage.UNGROUNDED: "knowledge_unavailable",
    Stage.FAILED: "fallback",
    Stage.REFUSED: "fallback",
    Stage.CLARIFICATION: "clarify_create",
}


def _allowed_key(stage: Stage) -> str:
    return _FALLBACK_KEY[stage]


class ReviewedDefectTest(ComposerFixture):
    """The stakeholder's reproductions from the slice 4b review, one test each."""

    def test_a_failed_action_cannot_borrow_success_wording(self):
        """The reproduction: a FAILED reply worded with `record_updated` claimed success."""
        with self.assertRaises(TemplateNotAllowed):
            self.composer.failed(self.update, "record_updated")

    def test_every_stage_enforces_its_own_template_allowlist(self):
        for stage, allowed in STAGE_TEMPLATES.items():
            with self.subTest(stage):
                forbidden = sorted(set(self.definition.responses) - allowed)
                if not forbidden:
                    continue
                with self.assertRaises(TemplateNotAllowed):
                    self.composer._render(stage, forbidden[0], {})

    def test_the_allowlists_never_let_a_non_executed_stage_assert_completion(self):
        completion_wording = {"record_created", "record_updated"}
        for stage, allowed in STAGE_TEMPLATES.items():
            if stage is Stage.EXECUTED:
                continue
            with self.subTest(stage):
                self.assertEqual(allowed & completion_wording, set())

    def test_an_executed_write_cannot_be_described_by_the_model(self):
        """The reproduction: closing one contact let the model claim a mass deletion."""
        reply = self.composer.from_model(
            "I deleted every customer and emailed their data.", Stage.EXECUTED, "record_updated",
            record_id="CON-1", changes="status to Closed",
        )
        self.assertTrue(reply.replaced_model_speech)
        self.assertEqual(reply.template_key, "record_updated")
        self.assertIn("CON-1", reply.speech)

    def test_a_retrieved_passage_does_not_license_an_unrelated_sentence(self):
        """The reproduction: a cycles passage made a Salesforce claim "grounded"."""
        import inspect

        signature = inspect.signature(ResponseComposer.knowledge_answer)
        self.assertEqual(set(signature.parameters), {"self", "grounding"},
                         "there is no way to hand a model sentence to a knowledge answer")

    def test_capability_filtering_fails_closed(self):
        """The reproduction: omitting the filters advertised every declared action."""
        import inspect

        signature = inspect.signature(offerable)
        self.assertEqual(signature.parameters["policy"].default, inspect.Parameter.empty)
        with self.assertRaises(TypeError):
            offerable(self.definition, desk_snapshot())

    def test_the_safe_policy_offers_nothing(self):
        offers = offerable(self.definition, desk_snapshot(), CapabilityPolicy.nothing())
        self.assertTrue(offers.is_empty)

    def test_grounding_freezes_the_passages_it_was_given(self):
        """The reproduction: mutating the original list changed whether an answer was grounded."""
        passages = [KnowledgePassage("Cycles", "docs/cycles.md", "Cycles are time-boxed.")]
        grounding = Grounding(passages)
        passages.clear()
        self.assertTrue(grounding.is_grounded)
        self.assertIsInstance(grounding.passages, tuple)

    def test_a_passage_without_a_source_or_snippet_is_refused(self):
        for passage in (KnowledgePassage("t", "", "snippet"), KnowledgePassage("t", "s", "  ")):
            with self.subTest(passage):
                with self.assertRaises(ValueError):
                    Grounding((passage,))

    def test_product_templates_cannot_rewrite_lifecycle_truth(self):
        document = engine_definition()
        document["responses"]["record_update_proposed"] = "Done. I deleted every customer."
        document["responses"]["fallback"] = "Everything changed successfully."
        composer = ResponseComposer(load_engine_definition(document=document))

        proposed = composer.proposed(self.update)
        failed = composer.failed(self.update)

        self.assertNotIn("deleted", proposed.speech.lower())
        self.assertNotIn("successfully", failed.speech.lower())
        self.assertIn("I'll update CON-1", proposed.speech)
        self.assertEqual(failed.speech, "I couldn't complete that request.")

    def test_a_document_completion_claim_is_explicitly_attributed(self):
        passage = KnowledgePassage(
            "Issue guide", "issues.md", "The ticket was closed and its assignee was changed."
        )
        reply = self.composer.knowledge_answer(Grounding((passage,)))
        self.assertEqual(
            reply.speech,
            "From the product documentation: The ticket was closed and its assignee was changed.",
        )
        self.assertEqual(reply.sources, ("issues.md",))

    def test_a_knowledge_answer_reads_as_a_sentence_not_a_quotation(self):
        """Wrapping the passage in quotation marks made every answer read like a citation."""
        passage = KnowledgePassage("Issue guide", "issues.md", "Tickets move quickly.")
        reply = self.composer.knowledge_answer(Grounding((passage,)))
        self.assertNotIn('"', reply.speech)
        self.assertNotIn("According to", reply.speech)


if __name__ == "__main__":
    unittest.main()
