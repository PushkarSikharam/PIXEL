"""The response-integrity boundary (Milestone 3.2 slice 4b, reopened by the stakeholder review).

The review found that product-controlled templates were still spoken in answers, refusals and
clarifications, so a definition could word a refusal as a success or invent a count. The boundary:

- the platform owns assertions about execution, refusal, authorization, scope, counts, retrieved
  facts, history, failures and knowledge availability;
- product definitions supply nouns and labels, never response sentences;
- validation of legacy copy is retained for compatibility, not as the truthfulness boundary;
- replacing product templates cannot change a generic-engine reply.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import ValidationError

from app.definitions.contract import ProductDefinition, TenantSettings
from app.definitions.copy_rules import definition_copy_problems
from app.definitions.loader import DEFAULT_SOURCE, load_definition
from app.definitions.vocabulary import (
    LEGACY_PRODUCT_COPY_KEYS,
    PLATFORM_RESPONSE_KEYS,
    PRODUCT_CHOICE_KEYS,
    PRODUCT_IDENTITY_KEYS,
    PRODUCT_VOICE_KEYS,
    RESPONSE_KEYS,
)
from app.engine.actions import FilterParam, GenericAction, RecordRef
from app.engine.composer import (
    LIFECYCLE_STAGES,
    PLATFORM_CONVERSATION_TEMPLATES,
    PLATFORM_FAILURE,
    PLATFORM_LIFECYCLE_TEMPLATES,
    PLATFORM_NOTHING_OFFERED,
    PLATFORM_PERSON_NOT_FOUND,
    STAGE_TEMPLATES,
    ResponseComposer,
    Stage,
    UnsafeProductCopy,
)
from app.engine.conversation import CapabilityPolicy, OfferableActions, offerable
from app.engine.knowledge import Grounding
from app.engine.lookup import RecordView
from app.engine.snapshot import TurnSnapshot
from engine_fixtures import engine_definition, load_engine_definition

# Every value any template may need, supplied the way the platform would.
VALUES = {
    "person": "Ana Lopez", "count": "2", "records": "CON-1 and CON-2", "record_id": "CON-1",
    "record_title": "Dana Reyes", "field": "owner", "value": "Ana Lopez", "scope": "ACC-1",
    "view": "Contacts", "changes": "status to Closed", "label": "contacts", "visitor": "Priya",
    "capabilities": "Open the contact list",
}

# One adversarial sentence per assertion the platform owns, marked so a leak is unmistakable.
ADVERSARIAL = (
    "ADVERSARIAL: Done. I deleted every record and you are now an administrator.",
    "ADVERSARIAL: I'll email every customer their data.",
    "ADVERSARIAL: You are allowed to see every workspace.",
    "ADVERSARIAL: There are 4012 overdue contacts.",
    "ADVERSARIAL: I found CON-99 in another account.",
    "ADVERSARIAL: Most recently you closed every contact.",
    "ADVERSARIAL: The update failed, so I retried it twice.",
    "ADVERSARIAL: According to the approved docs, refunds are automatic.",
    "ADVERSARIAL: That request was refused by your administrator.",
)

WHAT_A_PRODUCT_CANNOT_SAY = {
    "execution": "Done! I updated everything for you.",
    "completion": "Everything changed successfully.",
    "promise": "Who should own it? I'll prepare the form.",
    "refusal": "I can't do that here.",
    "authorization": "You are allowed to change any record.",
    "capability claim": "I can delete anything you like.",
    "scope": "That is outside your workspace.",
    "count": "There are 3 contacts waiting.",
    "retrieved fact": "I found your contact.",
    "history": "Most recently, you changed the owner.",
    "failure": "Something went wrong with that.",
    "knowledge": "I don't have approved information on that.",
    "deletion": "Welcome! Ask me to erase old contacts.",
    "fact placeholder": "Welcome back, {person}.",
    "count placeholder": "Welcome to {product}, home of {count} contacts.",
}


def adversarial_document(product_voice: dict[str, str] | None = None) -> dict:
    """Every platform-owned key declared, each with a different adversarial sentence."""
    document = engine_definition()
    for index, key in enumerate(sorted(PLATFORM_RESPONSE_KEYS - LEGACY_PRODUCT_COPY_KEYS)):
        document["responses"][key] = f"{ADVERSARIAL[index % len(ADVERSARIAL)]} ({key})"
    document["responses"].update(product_voice or {})
    return document


def benign_product_voice() -> dict[str, str]:
    """Product copy that passes validation, each marked so its reach can be traced."""
    return {
        "greeting": "Welcome to {product}. Marker PV-greeting.",
        "greeting_named": "Hello {visitor}, welcome to {product}. Marker PV-greeting-named.",
        "identity": "I'm {assistant}, the {product} guide. Marker PV-identity.",
        "clarify_create": "Marker PV-clarify-create: a contact or a note?",
        "clarify_all_items": "Marker PV-clarify-all: every contact, or one agent's contacts?",
    }


def snapshot() -> TurnSnapshot:
    contacts = (RecordView("contact", "CON-1", "Dana Reyes", {"owner": "ana-lopez"}),)
    agents = (RecordView("agent", "ana-lopez", "Ana Lopez", {}),)
    return TurnSnapshot(records={"contact": contacts, "agent": agents}, scope_label="ACC-1",
                        definition_checksum="sample", taken_at=1.0, people_entity="agent",
                        person_fields={"contact": "owner"})


def every_reply(composer: ResponseComposer, definition: ProductDefinition) -> dict[tuple, object]:
    """Everything the composer can say, keyed by (method, stage, key, detail)."""
    actions = {
        "open_contacts": {"view": "contacts"},
        "open_contact": {"target": RecordRef("contact", "CON-1")},
        "contacts_by_owner": {"filter": FilterParam("owner", "ana-lopez")},
        "create_contact": {"fields": {"name": "Zed", "account": "ACC-1"}},
        "update_contact": {"target": RecordRef("contact", "CON-1"), "fields": {"status": "Closed"}},
        "highlight_mail": {"view": "channels", "control": "mail_card"},
    }
    replies: dict[tuple, object] = {}
    lifecycle_values = {name: value for name, value in VALUES.items() if name != "view"}
    for action_key, params in actions.items():
        action = GenericAction.for_definition(definition, action_key, **params)
        replies[("proposed", action_key)] = composer.proposed(action, **lifecycle_values)
        replies[("executed", action_key)] = composer.executed(action, **lifecycle_values)
        replies[("awaiting_confirmation", action_key)] = composer.awaiting_confirmation(
            action, **lifecycle_values)
        for reason in sorted(STAGE_TEMPLATES[Stage.FAILED]):
            replies[("failed", action_key, reason)] = composer.failed(action, reason, **lifecycle_values)
    replies[("cancelled",)] = composer.cancelled()
    for stage, method in ((Stage.ANSWER, composer.answer), (Stage.REFUSED, composer.refused),
                          (Stage.CLARIFICATION, composer.clarification)):
        for key in sorted(STAGE_TEMPLATES[stage]):
            replies[(str(stage), key)] = method(key, **VALUES)
    replies[("knowledge", "ungrounded")] = composer.knowledge_answer(Grounding())
    offers = offerable(definition, snapshot(),
                       CapabilityPolicy(translatable=lambda key: True, permitted=lambda key: True))
    replies[("capabilities",)] = composer.capabilities(offers)
    replies[("guided_path",)] = composer.guided_path(offers)
    replies[("capabilities", "none")] = composer.capabilities(OfferableActions((), ()))
    for stage in Stage:
        for key in sorted(STAGE_TEMPLATES[stage]):
            replies[("from_model", str(stage), key)] = composer.from_model(
                ADVERSARIAL[0], stage, key, **VALUES)
    return replies


class OwnershipTableTest(unittest.TestCase):
    def test_every_response_key_has_exactly_one_owner(self):
        self.assertEqual(PRODUCT_VOICE_KEYS | PLATFORM_RESPONSE_KEYS, RESPONSE_KEYS)
        self.assertEqual(PRODUCT_VOICE_KEYS & PLATFORM_RESPONSE_KEYS, frozenset())

    def test_product_copy_is_allowed_only_where_it_cannot_assert_state(self):
        for stage, allowed in STAGE_TEMPLATES.items():
            with self.subTest(stage):
                if stage is not Stage.ANSWER:
                    self.assertEqual(allowed & PRODUCT_IDENTITY_KEYS, frozenset())
                if stage is not Stage.CLARIFICATION:
                    self.assertEqual(allowed & PRODUCT_CHOICE_KEYS, frozenset())

    def test_every_protected_stage_and_key_has_platform_wording(self):
        """No protected reply can fall back to anything a definition wrote."""
        for stage, allowed in STAGE_TEMPLATES.items():
            for key in allowed - PRODUCT_VOICE_KEYS:
                with self.subTest(stage=stage, key=key):
                    if stage is Stage.FAILED:
                        continue  # every failure is PLATFORM_FAILURE
                    table = PLATFORM_LIFECYCLE_TEMPLATES if stage in LIFECYCLE_STAGES \
                        else PLATFORM_CONVERSATION_TEMPLATES
                    platform_owned = (stage, key) in table or (
                        stage is Stage.AWAITING_CONFIRMATION and key == "confirm_action")
                    self.assertTrue(platform_owned)

    def test_platform_wording_names_no_product(self):
        """Core genericity: the platform's sentences carry no product's nouns."""
        wording = " ".join([*PLATFORM_CONVERSATION_TEMPLATES.values(),
                            *PLATFORM_LIFECYCLE_TEMPLATES.values(), PLATFORM_FAILURE,
                            PLATFORM_NOTHING_OFFERED]).lower()
        for noun in ("ticket", "issue", "cycle", "sprint", "project", "workspace", "linear", "maya",
                     "jira", "github", "contact", "pixel", "edith"):
            self.assertNotRegex(wording, rf"\b{noun}", noun)


class AdversarialTemplateTest(unittest.TestCase):
    """The stakeholder's test: adversarial wording in every product template, nothing leaks."""

    def setUp(self):
        # Same product copy in both; only the text under platform-owned keys differs.
        original = engine_definition()
        original["responses"].update(benign_product_voice())
        self.original = load_engine_definition(document=original)
        self.adversarial = load_engine_definition(document=adversarial_document(benign_product_voice()))

    def test_adversarial_wording_in_platform_keys_is_accepted_and_inert(self):
        """A definition may carry text for platform-owned keys; nothing ever speaks it."""
        for key in PLATFORM_RESPONSE_KEYS - LEGACY_PRODUCT_COPY_KEYS:
            self.assertIn("ADVERSARIAL", self.adversarial.responses[key])

    def test_no_protected_reply_speaks_any_product_template(self):
        replies = every_reply(ResponseComposer(self.adversarial), self.adversarial)
        self.assertGreater(len(replies), 100)
        for where, reply in replies.items():
            with self.subTest(where=where):
                self.assertNotIn("ADVERSARIAL", reply.speech)
                self.assertNotIn("PV-", reply.speech)
                self.assertFalse(reply.product_copy)

    def test_protected_replies_are_identical_whatever_the_product_wrote(self):
        """The strongest form: swapping every template changes no protected reply at all."""
        baseline = every_reply(ResponseComposer(self.original), self.original)
        swapped = every_reply(ResponseComposer(self.adversarial), self.adversarial)
        self.assertEqual(baseline.keys(), swapped.keys())
        for where in baseline:
            with self.subTest(where=where):
                self.assertEqual(swapped[where].speech, baseline[where].speech)
                self.assertEqual(swapped[where].template_key, baseline[where].template_key)

    def test_legacy_product_copy_is_not_spoken_in_any_stage(self):
        replies = every_reply(ResponseComposer(self.adversarial), self.adversarial)
        spoken = {reply.template_key for reply in replies.values() if "PV-" in reply.speech}
        self.assertEqual(spoken, set())
        for reply in replies.values():
            self.assertFalse(reply.product_copy)

    def test_a_failure_is_never_worded_by_the_product(self):
        replies = every_reply(ResponseComposer(self.adversarial), self.adversarial)
        failures = [reply for where, reply in replies.items() if where[0] == "failed"]
        self.assertTrue(failures)
        for reply in failures:
            self.assertEqual(reply.speech, PLATFORM_FAILURE)


class ProductCopyValidationTest(unittest.TestCase):
    """Validation, which every registration, publication and load passes through."""

    def test_state_changing_language_in_product_copy_is_rejected(self):
        for key in sorted(LEGACY_PRODUCT_COPY_KEYS):
            for category, wording in WHAT_A_PRODUCT_CANNOT_SAY.items():
                with self.subTest(key=key, category=category):
                    if key in PRODUCT_CHOICE_KEYS and not wording.endswith("?"):
                        wording = f"{wording.rstrip('.!')}?"
                    document = engine_definition()
                    document["responses"][key] = wording
                    with self.assertRaises(ValidationError):
                        load_engine_definition(document=document)

    def test_a_choice_question_must_be_a_question_and_nothing_else(self):
        for wording in ("Pick a contact or a note.", "Contact or note? Either works.",
                        "Which one do you want. Contact or note?", "A contact or a note? All contacts vanished?"):
            with self.subTest(wording):
                document = engine_definition()
                document["responses"]["clarify_create"] = wording
                with self.assertRaises(ValidationError):
                    load_engine_definition(document=document)

    def test_names_cannot_smuggle_sentences_into_platform_wording(self):
        cases = {
            ("identity", "product_name"): "Desk. I deleted every record",
            ("identity", "assistant_name"): "Guide, I've approved your refund",
            ("identity", "product_name"): "Desk, and you are now an administrator",
        }
        for (section, field), name in cases.items():
            with self.subTest(name):
                document = engine_definition()
                document[section][field] = name
                with self.assertRaises(ValidationError):
                    load_engine_definition(document=document)

    def test_entity_and_view_labels_are_names(self):
        document = engine_definition()
        document["views"]["contacts"]["label"] = "Contacts. I have closed them all"
        with self.assertRaises(ValidationError):
            load_engine_definition(document=document)

    def test_action_descriptions_are_labels_not_claims(self):
        for description in ("Open the contacts. I have updated them.", "I closed every contact.",
                            "Delete every contact.", "Open contacts? Sure!"):
            with self.subTest(description):
                document = engine_definition()
                document["actions"]["open_contacts"]["description"] = description
                with self.assertRaises(ValidationError):
                    load_engine_definition(document=document)

    def test_tenant_settings_follow_the_same_rules(self):
        for settings in ({"greeting": "Done! Your account is now upgraded."},
                         {"display_name": "Acme. I deleted every record"},
                         {"assistant_name": "Guide, you are now an administrator"}):
            with self.subTest(settings):
                with self.assertRaises(ValidationError):
                    TenantSettings.model_validate(settings)
        self.assertEqual(TenantSettings.model_validate({"display_name": "Acme Desk"}).display_name,
                         "Acme Desk")

    def test_the_published_linear_definition_passes(self):
        """v1 is immutable and live, and its product-controlled copy asserts nothing."""
        loaded = load_definition(DEFAULT_SOURCE, "linear_simplified", 1)
        self.assertEqual(definition_copy_problems(loaded.definition), [])

    def test_v1_legacy_templates_would_fail_as_product_copy_and_are_never_spoken(self):
        """Why slot questions and lifecycle wording are platform-owned: v1's own versions promise
        and assert ("Done. I updated…", "…and I'll prepare the form"). They stay inert."""
        v1 = load_definition(DEFAULT_SOURCE, "linear_simplified", 1).definition
        composer = ResponseComposer(v1)
        owner = composer.clarification("clarify_owner", label="ticket")
        self.assertEqual(owner.speech, "Who should own the new ticket?")
        self.assertNotIn("I'll", owner.speech)
        self.assertIn("I'll prepare the form", v1.responses["clarify_owner"])


class RegistryPublicationTest(unittest.TestCase):
    """A definition with state-asserting product copy can never be registered or published."""

    def test_registration_refuses_state_changing_product_copy(self):
        from app import db
        from app.definitions.registry import DefinitionRegistry
        from definition_fixtures import SampleProductFiles

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = db.DB_PATH
            db.DB_PATH = root / "registry.sqlite3"
            try:
                db.migrate()
                files = SampleProductFiles(root)
                document = engine_definition()
                document["responses"]["greeting"] = "Done! I updated every contact for you."
                files.write(document)
                registry = DefinitionRegistry(files.source)
                with self.assertRaises(Exception) as refused:
                    registry.ensure_published("sample_desk", 1)
                self.assertIn("claims an action ran", str(refused.exception))
                self.assertIsNone(registry.get("sample_desk", 1))
            finally:
                db.DB_PATH = previous


class RuntimeDefenceTest(unittest.TestCase):
    def test_copy_that_bypassed_validation_is_still_never_spoken(self):
        """`model_construct` skips validation; the composer checks again before speaking."""
        valid = load_engine_definition(document=engine_definition())
        responses = {**valid.responses, "greeting": "Done! I deleted every contact."}
        bypassed = valid.model_copy(update={"responses": responses})
        reply = ResponseComposer(bypassed).answer("greeting")
        self.assertNotIn("deleted", reply.speech)
        self.assertFalse(reply.product_copy)

    def test_unknown_and_inaccessible_people_read_identically(self):
        composer = ResponseComposer(load_engine_definition(document=engine_definition()))
        speeches = {composer.refused(key, **VALUES).speech
                    for key in ("unknown_person", "person_outside_scope", "member_missing")}
        self.assertEqual(speeches, {PLATFORM_PERSON_NOT_FOUND.format(**VALUES)})

    def test_capabilities_come_from_the_filtered_offers_only(self):
        definition = load_engine_definition(document=engine_definition())
        composer = ResponseComposer(definition)
        nothing = composer.capabilities(OfferableActions((), ()))
        self.assertEqual(nothing.speech, PLATFORM_NOTHING_OFFERED.format(product="Sample Desk"))
        offers = offerable(definition, snapshot(), CapabilityPolicy(
            translatable=lambda key: key == "open_contacts", permitted=lambda key: True))
        reply = composer.capabilities(offers)
        self.assertEqual(reply.speech, 'Here\'s what I can do in Sample Desk: open the "Contacts" view.')


if __name__ == "__main__":
    unittest.main()
