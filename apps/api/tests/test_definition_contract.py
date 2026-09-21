"""Milestone 3.1: the Product Definition contract and its security invariants.

Definitions are untrusted input. Anything outside the closed platform vocabulary, or any
parameter that could act as a URL, path, selector, markup or pattern, rejects the whole file.
"""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import typing
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.definitions import contract, vocabulary
from app.definitions.loader import DefinitionError, load_definition, parse_definition
from definition_fixtures import DEFINITION_ID, SampleProductFiles, sample_definition


class DefinitionContractTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.files = SampleProductFiles(Path(temporary.name))

    def load(self, document, version: int = 1):
        self.files.write(document, version)
        return load_definition(self.files.source, DEFINITION_ID, version)

    def assert_rejected(self, document, *fragments: str, version: int = 1):
        with self.assertRaises(DefinitionError) as rejected:
            self.load(document, version)
        for fragment in fragments:
            self.assertIn(fragment, str(rejected.exception))
        return rejected.exception

    # --- Valid definitions ---

    def test_valid_definition_loads_with_a_content_checksum(self):
        loaded = self.load(sample_definition())
        self.assertEqual(len(loaded.checksum), 64)
        self.assertEqual(loaded.definition.scope.paths["note"], ["contact", "account"])
        self.assertEqual(loaded.definition.actions["create_contact"].capability, vocabulary.Capability.CREATE_RECORD)

    def test_platform_views_can_be_navigated_but_not_redefined(self):
        self.load(sample_definition())
        document = sample_definition()
        document["views"]["architecture"] = {"label": "Mine", "kind": "dashboard"}
        self.assert_rejected(document, "reserved for the platform")

    # --- Capability boundary ---

    def test_unsupported_capabilities_are_rejected(self):
        for capability in ("RUN_COMMAND", "READ_FILE", "CALL_URL", "EXECUTE_SQL", "DELETE_RECORD", "navigate_view"):
            document = sample_definition()
            document["actions"]["escalate"] = {"capability": capability, "description": "Escalate."}
            with self.subTest(capability=capability):
                self.assert_rejected(document, "capability")

    def test_capabilities_only_accept_their_own_parameters(self):
        document = sample_definition()
        document["actions"]["open_contacts"]["fields"] = ["name"]
        self.assert_rejected(document, "does not accept fields")
        document = sample_definition()
        del document["actions"]["contacts_by_owner"]["by"]
        self.assert_rejected(document, "needs by")

    def test_the_vocabulary_has_no_delete_capability(self):
        self.assertFalse(any("DELETE" in capability for capability in vocabulary.Capability))

    # --- Parameter safety ---

    def test_targets_must_be_declared_keys(self):
        unsafe_targets = {
            "view": "https://evil.example/login",
            "control": "#login > button.primary",
        }
        for parameter, value in unsafe_targets.items():
            document = sample_definition()
            document["actions"]["highlight_mail"][parameter] = value
            with self.subTest(parameter=parameter):
                self.assert_rejected(document, parameter)

    def test_references_to_undeclared_keys_are_rejected(self):
        cases = {
            "view": ("open_contacts", "view", "billing", "unknown view"),
            "control": ("highlight_mail", "control", "sms_card", "has no control"),
            "field": ("create_contact", "fields", ["name", "secret"], "has no field"),
            "entity": ("open_contact", "entity", "invoice", "unknown entity"),
        }
        for label, (action, parameter, value, message) in cases.items():
            document = sample_definition()
            document["actions"][action][parameter] = value
            with self.subTest(label=label):
                self.assert_rejected(document, message)

    def test_html_and_unknown_placeholders_are_rejected_in_templates(self):
        for template in ("<b>Hello</b>", "Hello {secret}", "Hello {view", "Click javascript:alert(1)"):
            document = sample_definition()
            document["responses"]["view_opened"] = template
            with self.subTest(template=template):
                self.assert_rejected(document)

    def test_urls_and_paths_are_rejected_in_text(self):
        for text in ("See https://example.com", "Read ../../etc/passwd", "Open /var/data", "Visit www.example.com"):
            document = sample_definition()
            document["identity"]["persona"] = text
            with self.subTest(text=text):
                self.assert_rejected(document)

    def test_match_terms_are_literal_only(self):
        for term in ("contact.*", "^contact$", "[a-z]+", "contact|note", "Contact", "a  b"):
            document = sample_definition()
            document["intents"][0]["match"] = [[term]]
            with self.subTest(term=term):
                self.assert_rejected(document)

    def test_unknown_fields_are_rejected_everywhere(self):
        locations = [
            lambda d: d.update(scripts={"run": "x"}),
            lambda d: d["actions"]["open_contacts"].update(url="x"),
            lambda d: d["entities"]["contact"]["fields"]["name"].update(pattern="x"),
            lambda d: d["views"]["contacts"].update(selector="x"),
            lambda d: d["intents"][0].update(regex="x"),
        ]
        for index, mutate in enumerate(locations):
            document = sample_definition()
            mutate(document)
            with self.subTest(location=index):
                self.assert_rejected(document, "Extra inputs are not permitted")

    def test_unknown_response_keys_are_rejected_and_platform_questions_need_no_copy(self):
        document = sample_definition()
        document["responses"]["run_shell"] = "Hello."
        self.assert_rejected(document, "unknown response key")
        # The platform supplies the question; legacy product copy is optional.
        document = sample_definition()
        del document["responses"]["clarify_create"]
        self.assertNotIn("clarify_create", self.load(document).definition.responses)

    def test_platform_owned_replies_need_no_product_wording(self):
        """The guardrail refusal is platform wording (the 4b response boundary)."""
        document = sample_definition()
        del document["responses"]["out_of_scope"]
        self.assertNotIn("out_of_scope", self.load(document).definition.responses)

    # --- Structure ---

    def test_scope_paths_are_limited_to_two_hops(self):
        document = sample_definition()
        document["scope"]["paths"]["note"] = ["contact", "account", "account"]
        self.assert_rejected(document, "scope")

    def test_scope_paths_must_reach_the_anchor_through_references(self):
        cases = {
            "wrong end": ["contact", "owner"],
            "not a reference": ["body"],
        }
        for label, path in cases.items():
            document = sample_definition()
            document["scope"]["paths"]["note"] = path
            with self.subTest(label=label):
                self.assert_rejected(document, "scope")
        document = sample_definition()
        del document["scope"]["paths"]["note"]
        self.assert_rejected(document, "has no path")

    def test_updates_cannot_target_read_only_fields(self):
        document = sample_definition()
        document["actions"]["rename_agent"] = {
            "capability": "UPDATE_RECORD", "entity": "agent", "fields": ["name"], "description": "Rename.",
        }
        self.assert_rejected(document, "not editable")

    def test_reference_fields_need_a_declared_target(self):
        document = sample_definition()
        document["entities"]["note"]["fields"]["contact"]["target"] = "ticket_queue"
        self.assert_rejected(document, "unknown target entity")

    # --- File identity and parsing ---

    def test_declared_identity_must_match_the_file_path(self):
        self.assert_rejected(sample_definition(version=2), "declares", version=1)
        document = sample_definition()
        document["definition"]["definition_id"] = "other_desk"
        self.assert_rejected(document, "declares")

    def test_ownership_names_an_owner_only_for_private_definitions(self):
        self.assertEqual(self.load(sample_definition(owner_organization="acme")).definition.definition.owner_organization, "acme")
        shared_with_owner = sample_definition()
        shared_with_owner["definition"]["owner_organization"] = "acme"
        self.assert_rejected(shared_with_owner, "only those, name an owner")
        private_without_owner = sample_definition(owner_organization="acme")
        del private_without_owner["definition"]["owner_organization"]
        self.assert_rejected(private_without_owner, "only those, name an owner")
        unknown = sample_definition()
        unknown["definition"]["ownership"] = "public"
        self.assert_rejected(unknown, "ownership")

    def test_definitions_do_not_carry_organization_or_product_bindings(self):
        for key, value in (("tenant_id", "acme"), ("product_id", "acme-desk"), ("team_id", "support")):
            with self.subTest(key=key):
                document = sample_definition()
                document["definition"][key] = value
                self.assert_rejected(document, "Extra inputs are not permitted")

    def test_yaml_aliases_and_repeated_keys_are_rejected(self):
        with_alias = "anchors: &name [a]\ncopy: *name\n"
        with self.assertRaises(DefinitionError):
            parse_definition(with_alias.encode())
        repeated = "definition: {product_id: sample_desk, version: 1}\ndefinition: {product_id: x, version: 2}\n"
        with self.assertRaises(DefinitionError):
            parse_definition(repeated.encode())

    def test_oversized_files_are_rejected(self):
        self.assert_rejected("# " + "x" * 300_000 + "\n", "exceeds")

    def test_invalid_definition_ids_cannot_reach_the_file_system(self):
        with self.assertRaises(DefinitionError):
            load_definition(self.files.source, "../secrets", 1)

    # --- Adapter registry ---

    def test_adapter_views_need_a_registered_adapter(self):
        self.files.write_manifest(None)
        self.assert_rejected(sample_definition(), "needs a registered adapter")

    def test_adapter_views_cannot_invent_controls(self):
        document = sample_definition()
        document["views"]["channels"]["controls"]["sms_card"] = {"label": "SMS"}
        self.assert_rejected(document, "does not render")

    # --- Platform vocabulary consistency ---

    def test_contract_types_match_the_platform_vocabulary(self):
        self.assertEqual(set(contract.TenantSettings.model_fields), set(vocabulary.TENANT_SETTING_KEYS))
        self.assertEqual(set(typing.get_args(contract.IntentRequirement)), set(vocabulary.INTENT_REQUIREMENTS))


if __name__ == "__main__":
    unittest.main()
