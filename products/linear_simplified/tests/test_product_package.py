"""This product's record lookup and legacy translator (3.2 plan, sections 7 and 9).

Two things are proven here. The lookup's scope is applied before anything is matched, counted
or offered, so a caller sees only their own workspaces and cannot widen that through any
argument. And the translator is total: every action the definition declares has one legacy
mapping, and anything it cannot express raises instead of guessing.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from app import db  # noqa: E402
from app.definitions.loader import DEFAULT_SOURCE, load_definition  # noqa: E402
from app.engine.actions import FilterParam, GenericAction, RecordRef  # noqa: E402
from app.engine.lookup import RecordLookup  # noqa: E402
from app.engine.validator import ValidatedAction  # noqa: E402
from app.record_access import RecordGrant  # noqa: E402
from app.services import env as env_module  # noqa: E402
from app.services.product_data_store import ProductDataStore  # noqa: E402
from app.engine.knowledge import KnowledgeContext, KnowledgeLookup  # noqa: E402
from app.engine.snapshot import SnapshotSource, TurnSnapshot, take_snapshot  # noqa: E402
from products.linear_simplified.backend.lookup import (  # noqa: E402
    PEOPLE_ENTITY,
    PERSON_FIELDS,
    LinearLegacyLookup,
    lookup_for,
)
from products.linear_simplified.backend.knowledge import (  # noqa: E402
    LinearKnowledgeLookup,
    knowledge_for,
)
from products.linear_simplified.backend.package import PACKAGE  # noqa: E402
from products.linear_simplified.backend.translator import (  # noqa: E402
    LEGACY_TYPES,
    LinearLegacyTranslator,
    TranslationMissing,
)

PRODUCT_ENG = "workspace-product-eng"
PLATFORM = "workspace-platform"


class ProductRecordFixture(unittest.TestCase):
    def setUp(self):
        files_patch = patch.object(env_module, "_env_files", lambda: ())
        files_patch.start()
        self.addCleanup(files_patch.stop)
        env_patch = patch.dict(os.environ, {"PIXEL_SYNTHETIC_DEMO": "true", "PIXEL_DEMO_SEEDS": "true"})
        env_patch.start()
        self.addCleanup(env_patch.stop)

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        db_patch = patch.object(db, "DB_PATH", Path(temporary.name) / "product.sqlite3")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        db.migrate()

        self.store = ProductDataStore()
        self.store.seed_if_empty()
        self.definition = load_definition(DEFAULT_SOURCE, "linear_simplified", 1).definition

    def lookup(self, *scopes: str) -> LinearLegacyLookup:
        return LinearLegacyLookup(self.store, frozenset(scopes))

    def admin_lookup(self) -> LinearLegacyLookup:
        return LinearLegacyLookup(self.store, None)

    def snapshot(self, *scopes: str) -> TurnSnapshot:
        source = LinearLegacyLookup(self.store, frozenset(scopes) if scopes else None)
        return take_snapshot(
            source, definition_checksum="test", people_entity=PEOPLE_ENTITY,
            person_fields=PERSON_FIELDS,
        )


class LookupScopeTest(ProductRecordFixture):
    def test_the_lookup_satisfies_the_engines_protocol(self):
        self.assertIsInstance(self.lookup(PRODUCT_ENG), RecordLookup)

    def test_only_records_in_the_callers_workspaces_are_counted(self):
        narrow = self.lookup(PRODUCT_ENG).count("issue")
        wide = self.admin_lookup().count("issue")
        self.assertGreater(narrow, 0)
        self.assertGreater(wide, narrow, "an unscoped caller must see more than a scoped one")

    def test_a_record_in_another_workspace_cannot_be_fetched_by_id(self):
        other = self.admin_lookup()
        elsewhere = next(
            record for record in [other.get("issue", row["id"]) for row in self.store.load()["issues"]]
            if record is not None and self.lookup(PRODUCT_ENG).get("issue", record.id) is None
        )
        self.assertIsNone(self.lookup(PRODUCT_ENG).get("issue", elsewhere.id))
        self.assertIsNotNone(other.get("issue", elsewhere.id), "the record does exist, just not for this caller")

    def test_search_never_returns_a_record_from_another_workspace(self):
        scoped, everything = self.lookup(PRODUCT_ENG), self.admin_lookup()
        for entity in ("issue", "project", "cycle", "member"):
            with self.subTest(entity):
                self.assertEqual(scoped.search(entity, "", 100), [], "an empty search matches nothing")
                found = {record.id for record in scoped.search(entity, "e", 100)}
                allowed = {record.id for record in scoped._records(entity)}
                self.assertTrue(found <= allowed)
                # A title only the wider caller can see is not findable here.
                for record in everything._records(entity):
                    if record.id not in allowed and record.title:
                        self.assertEqual(scoped.search(entity, record.title, 5), [])

    def test_people_outside_the_workspace_are_never_offered(self):
        scoped = self.lookup(PRODUCT_ENG)
        self.assertIsNotNone(scoped.people("Maya Chen", 3).unique)
        # Someone who only works in the platform workspace is not a candidate here.
        platform_only = [
            person.name for person in self.admin_lookup().people("", 50).matches
        ]
        self.assertEqual(platform_only, [], "an empty name matches nobody")
        outsiders = {
            record.title for record in self.admin_lookup()._records("member")
        } - {record.title for record in scoped._records("member")}
        self.assertTrue(outsiders, "the seed data must contain someone outside this workspace")
        for name in outsiders:
            self.assertEqual(scoped.people(name, 3).matches, (), f"{name} must not be offered")

    def test_filtering_by_a_person_only_returns_visible_records(self):
        scoped = self.lookup(PRODUCT_ENG)
        for record in scoped.by_person("issue", "maya-chen", 50):
            self.assertIsNotNone(scoped.get("issue", record.id))

    def test_an_entity_the_product_does_not_have_is_simply_empty(self):
        scoped = self.lookup(PRODUCT_ENG)
        self.assertEqual(scoped.count("invoice"), 0)
        self.assertIsNone(scoped.get("invoice", "INV-1"))
        self.assertEqual(scoped.search("invoice", "anything", 5), [])

    def test_no_lookup_method_takes_a_scope(self):
        """Scope is fixed when the lookup is built; no caller can pass one in."""
        import inspect

        for name in ("get", "search", "by_person", "people", "count"):
            parameters = set(inspect.signature(getattr(LinearLegacyLookup, name)).parameters)
            self.assertFalse(
                parameters & {"scope", "scope_id", "scope_ids", "workspace", "workspace_scope_id"},
                f"{name} must not accept a scope",
            )

    def test_a_grant_builds_a_lookup_bound_to_exactly_its_scopes(self):
        member = lookup_for(RecordGrant(frozenset({PRODUCT_ENG}), False), self.store)
        administrator = lookup_for(RecordGrant(frozenset(), True), self.store)
        self.assertLess(member.count("issue"), administrator.count("issue"))

    def test_new_records_appear_within_the_scope_that_owns_them(self):
        before = self.lookup(PRODUCT_ENG).count("issue")
        self.store.save_issue({
            "id": "LIN-950", "title": "Scoped addition", "priority": "Low", "assignee": "Maya Chen",
            "project": "Integrations", "status": "Todo",
        })
        self.assertEqual(self.lookup(PRODUCT_ENG).count("issue"), before + 1)
        self.assertIsNone(self.lookup(PLATFORM).get("issue", "LIN-950"))


class SnapshotSourceTest(ProductRecordFixture):
    """The product reads a turn's records once, through the caller's transaction (plan 7.1)."""

    def test_the_lookup_is_a_snapshot_source(self):
        self.assertIsInstance(self.lookup(PRODUCT_ENG), SnapshotSource)

    def test_a_snapshot_holds_only_the_callers_records(self):
        snapshot = self.snapshot(PRODUCT_ENG)
        self.assertEqual(snapshot.count("issue"), self.lookup(PRODUCT_ENG).count("issue"))
        self.assertGreater(self.snapshot().count("issue"), snapshot.count("issue"))

    def test_every_entity_comes_from_one_read(self):
        """A record changed after the snapshot cannot change what the turn already answered."""
        snapshot = self.snapshot(PRODUCT_ENG)
        before = snapshot.get("issue", "LIN-142").title
        self.store.update_issue("LIN-142", {**self.store.get_issue("LIN-142"), "title": "Changed"})
        self.assertEqual(snapshot.get("issue", "LIN-142").title, before)
        self.assertEqual(self.snapshot(PRODUCT_ENG).get("issue", "LIN-142").title, "Changed")

    def test_the_snapshot_resolves_people_the_way_the_lookup_does(self):
        snapshot = self.snapshot(PRODUCT_ENG)
        self.assertEqual(snapshot.people("Maya Chen", 3).unique.id, "maya-chen")
        self.assertEqual(snapshot.people("Sam Rivera", 3).matches, ())

    def test_the_snapshot_records_the_scope_it_was_bound_to(self):
        self.assertEqual(self.snapshot(PRODUCT_ENG).scope_label, "Product Engineering Workspace")
        self.assertEqual(self.snapshot().scope_label, "all workspaces")

    def test_the_product_reads_inside_the_snapshots_transaction(self):
        """One moment for every entity, and the caller's transaction is required."""
        with closing(sqlite3.connect(db.DB_PATH)) as connection:
            connection.row_factory = sqlite3.Row
            with self.assertRaises(RuntimeError):
                take_snapshot(self.lookup(PRODUCT_ENG), definition_checksum="x",
                              connection=connection)
            connection.execute("begin")
            snapshot = take_snapshot(self.lookup(PRODUCT_ENG), definition_checksum="x",
                                     connection=connection)
            self.assertGreater(snapshot.count("issue"), 0)
            connection.rollback()


class TranslatorTest(ProductRecordFixture):
    def setUp(self):
        super().setUp()
        self.translator = LinearLegacyTranslator(self.lookup(PRODUCT_ENG))

    def validated(self, action_key: str, **params) -> ValidatedAction:
        action = GenericAction.for_definition(self.definition, action_key, **params)
        return ValidatedAction(action, "linear_simplified", 1)

    def translate(self, action_key: str, **params):
        return self.translator.translate(self.validated(action_key, **params))

    UNTRANSLATABLE = {"create_member": "today's app can only highlight where members are added"}

    def test_every_action_either_has_a_mapping_or_is_declared_untranslatable(self):
        """No action may be missing by accident, and none may be quietly downgraded."""
        self.assertEqual(
            sorted(self.definition.actions), sorted({*LEGACY_TYPES, *self.UNTRANSLATABLE}),
            "an action without a mapping would be silently undeliverable",
        )
        self.assertEqual(set(LEGACY_TYPES) & set(self.UNTRANSLATABLE), set())
        for action_key in self.UNTRANSLATABLE:
            with self.subTest(action_key):
                self.assertNotIn(action_key, LEGACY_TYPES)

    def test_each_navigation_maps_to_its_own_legacy_type(self):
        for action_key, view in (("open_dashboard", "dashboard"), ("open_issues", "issues"),
                                 ("open_projects", "projects"), ("open_cycles", "cycles"),
                                 ("open_teams", "teams"), ("open_integrations", "integrations"),
                                 ("open_architecture", "architecture")):
            with self.subTest(action_key):
                legacy = self.translate(action_key, view=view)
                self.assertEqual(legacy.type, LEGACY_TYPES[action_key])
                self.assertEqual(legacy.payload, {})

    def test_opening_a_record_carries_its_id(self):
        legacy = self.translate("open_issue", target=RecordRef("issue", "LIN-142"))
        self.assertEqual((legacy.type, legacy.payload), ("OPEN_DEMO_ISSUE", {"issue_id": "LIN-142"}))

    def test_filtering_by_a_person_carries_the_display_name(self):
        legacy = self.translate("issues_by_assignee", filter=FilterParam("assignee", "maya-chen"))
        self.assertEqual((legacy.type, legacy.payload),
                         ("FILTER_ISSUES_BY_ASSIGNEE", {"assignee": "Maya Chen"}))

    def test_creating_a_record_carries_every_field(self):
        legacy = self.translate("create_issue", fields={
            "title": "New work", "priority": "High", "assignee": "maya-chen",
            "project": "Integrations", "status": "Todo",
        })
        self.assertEqual(legacy.type, "CREATE_DEMO_ISSUE")
        self.assertEqual(legacy.payload, {
            "title": "New work", "priority": "High", "assignee": "Maya Chen",
            "project": "Integrations", "status": "Todo",
        })

    def test_updating_a_record_carries_the_target_and_only_the_changes(self):
        legacy = self.translate("update_issue", target=RecordRef("issue", "LIN-142"),
                                fields={"priority": "Low"})
        self.assertEqual((legacy.type, legacy.payload),
                         ("UPDATE_DEMO_ISSUE", {"issue_id": "LIN-142", "priority": "Low"}))

    def test_a_highlight_with_a_record_carries_it(self):
        legacy = self.translate("highlight_assignment", view="issue_detail",
                                control="assignment_control", target=RecordRef("issue", "LIN-142"))
        self.assertEqual((legacy.type, legacy.payload),
                         ("HIGHLIGHT_ASSIGNMENT_CONTROL", {"issue_id": "LIN-142"}))

    def test_a_highlight_with_a_prepared_draft_carries_the_prefill(self):
        legacy = self.translate("highlight_create_issue", view="issues", control="create_ticket_button",
                                prefill={"title": "Draft", "assignee": "maya-chen"})
        self.assertEqual((legacy.type, legacy.payload),
                         ("HIGHLIGHT_CREATE_TICKET_BUTTON", {"title": "Draft", "assignee": "Maya Chen"}))

    def test_a_highlight_without_a_prefill_carries_nothing(self):
        legacy = self.translate("highlight_add_member", view="teams", control="add_member_button")
        self.assertEqual((legacy.type, legacy.payload), ("HIGHLIGHT_ADD_MEMBER_BUTTON", {}))

    def test_the_integration_highlights_each_map_separately(self):
        for action_key, control in (("open_github_setup", "github_setup"), ("highlight_github", "github_card"),
                                    ("highlight_slack", "slack_card")):
            with self.subTest(action_key):
                legacy = self.translate(action_key, view="integrations", control=control)
                self.assertEqual(legacy.type, LEGACY_TYPES[action_key])

    def test_creating_a_member_is_refused_rather_than_downgraded_to_a_highlight(self):
        """Today's app cannot create a member, and a highlight is not a create."""
        with self.assertRaises(TranslationMissing):
            self.translate("create_member", fields={"name": "Maya Chen"})

    def test_a_prepared_member_name_is_text_not_a_person_reference(self):
        """The person does not exist yet, so the name is carried as written."""
        legacy = self.translate("highlight_add_member", view="teams", control="add_member_button",
                                prefill={"name": "Someone New"})
        self.assertEqual((legacy.type, legacy.payload),
                         ("HIGHLIGHT_ADD_MEMBER_BUTTON", {"name": "Someone New"}))

    def test_an_action_outside_the_table_is_never_guessed(self):
        action = GenericAction("export_everything", "NAVIGATE_VIEW", view="issues")
        with self.assertRaises(TranslationMissing):
            self.translator.translate(ValidatedAction(action, "linear_simplified", 1))

    def test_a_person_the_caller_cannot_see_cannot_be_translated(self):
        """The scope boundary holds on this path too: no name is revealed."""
        outsider = next(
            record.id for record in self.admin_lookup()._records("member")
            if self.lookup(PRODUCT_ENG).get("member", record.id) is None
        )
        with self.assertRaises(TranslationMissing):
            self.translate("issues_by_assignee", filter=FilterParam("assignee", outsider))

    def test_a_field_with_no_legacy_key_raises_instead_of_dropping_it(self):
        """Silently dropping a field would send a different change than the one approved."""
        translator = LinearLegacyTranslator(self.lookup(PRODUCT_ENG))

        class Fabricated:
            action_key = "create_issue"
            fields = {"invented": "value"}
            prefill = None
            target = None
            filter = None

        with self.assertRaises(TranslationMissing):
            translator._fields(Fabricated(), Fabricated.fields)


class PackageRegistrationTest(ProductRecordFixture):
    def test_the_package_registers_both_factories_in_code(self):
        self.assertEqual(PACKAGE.definition_id, "linear_simplified")
        self.assertIs(PACKAGE.lookup_factory, lookup_for)
        self.assertIsNotNone(PACKAGE.legacy_translator)

    def test_the_platform_finds_this_package_by_definition_id(self):
        from app.installed_products import package_for

        self.assertIs(package_for("linear_simplified"), PACKAGE)


DEFINITION_ID = "linear_simplified"


def knowledge_context(definition_id: str = DEFINITION_ID) -> KnowledgeContext:
    """A caller's knowledge context. Every field is carried, whatever this implementation reads."""
    return KnowledgeContext(
        tenant_id="pixel-dev", product_id="linear-demo", definition_id=definition_id,
        definition_version=1, definition_checksum="a" * 64, knowledge_version=1,
        scope_label=PRODUCT_ENG,
    )


class KnowledgeSourceTest(ProductRecordFixture):
    """This product's knowledge lookup: bound to one product, read-only, honest when empty."""

    def test_the_lookup_satisfies_the_platform_protocol(self):
        self.assertIsInstance(knowledge_for(knowledge_context()), KnowledgeLookup)

    def test_it_finds_passages_for_a_product_question(self):
        passages = knowledge_for(knowledge_context()).search("how do cycles work", 2)
        self.assertTrue(passages, "the product ships documents about its own features")
        for passage in passages:
            self.assertTrue(passage.title and passage.source and passage.snippet)

    def test_an_unknown_definition_finds_nothing_rather_than_guessing(self):
        self.assertEqual(knowledge_for(knowledge_context("not_a_definition")).search("cycles", 2), [])

    def test_documents_are_keyed_by_definition_not_by_product(self):
        """A property of today's static documents, not of the boundary: 3.4 changes this."""
        self.assertEqual(
            knowledge_for(knowledge_context("linear_demo")).search("how do cycles work", 2), []
        )
        self.assertTrue(knowledge_for(knowledge_context()).search("how do cycles work", 2))

    def test_the_lookup_carries_the_whole_caller_context(self):
        """The boundary carries organization, product, version and scope from the start."""
        context = knowledge_context()
        lookup = knowledge_for(context)
        self.assertEqual(lookup.context.tenant_id, "pixel-dev")
        self.assertEqual(lookup.context.product_id, "linear-demo")
        self.assertEqual(lookup.context.definition_version, 1)
        self.assertEqual(lookup.context.definition_checksum, "a" * 64)
        self.assertEqual(lookup.context.scope_label, PRODUCT_ENG)

    def test_a_context_without_an_organization_is_refused(self):
        with self.assertRaises(ValueError):
            KnowledgeContext(tenant_id="", product_id="linear-demo", definition_id=DEFINITION_ID,
                             definition_version=1, definition_checksum="a" * 64,
                             knowledge_version=1, scope_label=PRODUCT_ENG)

    def test_malformed_identity_versions_checksum_and_scope_are_refused(self):
        valid = dict(
            tenant_id="pixel-dev", product_id="linear-demo", definition_id=DEFINITION_ID,
            definition_version=1, definition_checksum="a" * 64, knowledge_version=1,
            scope_label=PRODUCT_ENG,
        )
        bad_values = (
            {"tenant_id": " "},
            {"product_id": "not valid"},
            {"definition_id": ""},
            {"definition_version": 0},
            {"definition_version": True},
            {"knowledge_version": -1},
            {"definition_checksum": "checksum"},
            {"scope_label": ""},
        )
        for change in bad_values:
            with self.subTest(change):
                with self.assertRaises(ValueError):
                    KnowledgeContext(**{**valid, **change})

    def test_search_takes_no_product_argument(self):
        import inspect

        parameters = set(inspect.signature(LinearKnowledgeLookup.search).parameters)
        self.assertEqual(parameters, {"self", "text", "limit"})

    def test_an_empty_question_finds_nothing(self):
        self.assertEqual(knowledge_for(knowledge_context()).search("   ", 2), [])

    def test_the_limit_is_respected(self):
        self.assertLessEqual(len(knowledge_for(knowledge_context()).search("cycles issues projects", 1)), 1)

    def test_the_package_registers_the_knowledge_factory_in_code(self):
        self.assertIs(PACKAGE.knowledge_factory, knowledge_for)


if __name__ == "__main__":
    unittest.main()
