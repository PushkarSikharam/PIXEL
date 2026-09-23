"""A product that ships no code of its own is served anyway (3.5).

This is the gate for Pixel being a platform rather than one product with a chatbot attached.
Before this, every product needed an installed Python package before it could hold a single
conversation, so adding a product was a developer's job with a deployment attached, and a person
signing in to Pixel could not add one at all.

The product used here exists only as a definition and some rows: no package, no lookup, no
translator, nothing written for it. Everything asserted below - what its records are, what may be
changed, what its identifiers look like, who its people are, which scope a record falls in - is
read from its own definition.
"""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import db
from app.definitions.contract import ProductDefinition
from app.engine.conversation import CapabilityPolicy
from app.engine.conversation_engine import ConversationEngine
from app.engine.memory import ConversationMemory
from app.engine.router import TurnContext
from app.engine.signals import SignalHistory
from app.engine.snapshot import TurnSnapshot
from app.record_access import RecordGrant
from app.services.generic_package import client_action_type, package_from
from app.services.record_store import PRIMARY, RecordConflict, RecordInvalid, RecordStore
from library_fixtures import library_definition

TENANT, PRODUCT = "a-customer", "their-product"


class ProductWithoutCodeFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = patch.object(db, "DB_PATH", Path(temporary.name) / "records.sqlite3")
        database.start()
        self.addCleanup(database.stop)
        db.migrate()
        self.definition = ProductDefinition.model_validate(library_definition())
        self.store = RecordStore(TENANT, PRODUCT, PRIMARY)
        self.package = package_from(self.definition, TENANT, PRODUCT)

    def add_people_and_records(self) -> None:
        self.rosa = self.store.create(self.definition, "librarian", {"name": "Rosa Vale"})
        self.otto = self.store.create(self.definition, "librarian", {"name": "Otto Lind"})
        self.tide = self.store.create(self.definition, "book", {
            "title": "Tide Tables", "status": "On shelf", "keeper": self.rosa.id})
        self.almanac = self.store.create(self.definition, "book", {
            "title": "Winter Almanac", "status": "On loan", "keeper": self.otto.id})
        # This definition puts a librarian in scope through the books they keep, so the records
        # have to say which those are. The path is the definition's; the platform follows it and
        # never guesses a wider one.
        self.store.update(self.definition, "librarian", self.rosa.id, {"books": [self.tide.id]})
        self.store.update(self.definition, "librarian", self.otto.id, {"books": [self.almanac.id]})

    def engine(self, grant: RecordGrant) -> ConversationEngine:
        """The engine as the platform assembles it: this product's own lookup and translator,
        and a policy that offers only what the client can express."""
        lookup = self.package.lookup_factory(grant)
        translator = self.package.legacy_translator(lookup)
        records = lookup.records_from({})
        people = self.definition.people
        snapshot = TurnSnapshot(
            records=records, scope_label=lookup.scope_label, definition_checksum="test",
            taken_at=time.time(), people_entity=people.entity if people else None,
            person_fields={"book": "keeper"},
        )
        policy = CapabilityPolicy(translatable=translator.can_translate, permitted=lambda key: True)
        return ConversationEngine(self.definition, snapshot, policy, definition_version=1,
                                  translate=translator.translate)

    def say(self, message: str, grant: RecordGrant | None = None):
        engine = self.engine(grant or RecordGrant(frozenset(), is_admin=True))
        return engine.turn(message, ConversationMemory(), SignalHistory(), TurnContext(turn=1))


class ConversationTest(ProductWithoutCodeFixture):
    def test_a_product_with_no_package_answers_from_its_own_definition(self):
        turn = self.say("what can you do")
        self.assertIn("Sample Library", turn.reply.speech)

    def test_an_empty_product_offers_only_what_it_can_actually_do(self):
        """With no records yet, opening one is not offered: the platform never advertises an
        action it would have to refuse."""
        empty = self.say("what can you do").reply.speech
        self.assertNotIn("open a book", empty)
        self.add_people_and_records()
        filled = self.say("what can you do").reply.speech
        self.assertIn("open a book", filled)
        self.assertIn("list books by keeper", filled)

    def test_it_navigates_counts_and_opens_its_own_records(self):
        self.add_people_and_records()
        opened = self.say("show me the books")
        self.assertEqual(opened.validated.action.action_key, "open_catalogue")
        counted = self.say("how many books are there")
        self.assertIn("Tide Tables", counted.reply.speech)
        one = self.say(f"open {self.tide.id}")
        self.assertEqual(one.validated.action.action_key, "open_book")
        self.assertEqual(one.validated.action.target.id, self.tide.id)

    def test_it_resolves_its_own_people(self):
        self.add_people_and_records()
        turn = self.say("books for Rosa")
        self.assertEqual(turn.validated.action.action_key, "books_by_keeper")
        self.assertIn("Rosa Vale", turn.reply.speech)

    def test_it_refuses_by_its_own_guardrail(self):
        self.add_people_and_records()
        turn = self.say("delete everything")
        self.assertIsNone(turn.validated)
        self.assertIn("delete", turn.reply.speech.lower())

    def test_the_action_sent_is_named_for_the_action_the_definition_declares(self):
        self.add_people_and_records()
        turn = self.say("show me the books")
        self.assertEqual(client_action_type("open_catalogue"), "OPEN_CATALOGUE")
        self.assertIn("OPEN_CATALOGUE", self.package.client_action_types)
        self.assertEqual(
            self.package.client_action_types,
            frozenset(key.upper() for key in self.definition.actions),
        )


class RecordRulesComeFromTheDefinitionTest(ProductWithoutCodeFixture):
    def test_a_required_field_must_be_given(self):
        with self.assertRaises(RecordInvalid) as refused:
            self.store.create(self.definition, "book", {"title": "Nobody keeps it"})
        self.assertIn("keeper", str(refused.exception))

    def test_a_field_the_definition_gives_a_default_need_not_be_given(self):
        keeper = self.store.create(self.definition, "librarian", {"name": "Rosa Vale"})
        book = self.store.create(self.definition, "book", {"title": "Quiet", "keeper": keeper.id})
        self.assertEqual(book.fields["status"], "On shelf")

    def test_an_enum_only_takes_its_declared_values(self):
        keeper = self.store.create(self.definition, "librarian", {"name": "Rosa Vale"})
        with self.assertRaises(RecordInvalid):
            self.store.create(self.definition, "book", {
                "title": "Bad status", "status": "Eaten", "keeper": keeper.id})

    def test_an_unknown_field_is_refused_rather_than_stored(self):
        with self.assertRaises(RecordInvalid) as refused:
            self.store.create(self.definition, "librarian", {"name": "Rosa Vale", "salary": 1})
        self.assertIn("salary", str(refused.exception))

    def test_a_field_the_definition_calls_uneditable_cannot_be_changed(self):
        keeper = self.store.create(self.definition, "librarian", {"name": "Rosa Vale"})
        with self.assertRaises(RecordInvalid):
            self.store.update(self.definition, "librarian", keeper.id, {"name": "Someone else"})

    def test_a_reference_must_point_at_a_record_that_exists(self):
        with self.assertRaises(RecordConflict):
            self.store.create(self.definition, "book", {
                "title": "Orphan", "status": "On shelf", "keeper": "nobody"})

    def test_identifiers_are_minted_the_way_the_definition_asks(self):
        keeper = self.store.create(self.definition, "librarian", {"name": "Rosa Vale"})
        self.assertEqual(keeper.id, "rosa-vale", "a slug id comes from its source field")
        first = self.store.create(self.definition, "book", {
            "title": "One", "status": "On shelf", "keeper": keeper.id})
        second = self.store.create(self.definition, "book", {
            "title": "Two", "status": "On shelf", "keeper": keeper.id})
        self.assertEqual((first.id, second.id), ("BK-1", "BK-2"))

    def test_two_people_with_one_name_do_not_collide(self):
        first = self.store.create(self.definition, "librarian", {"name": "Rosa Vale"})
        second = self.store.create(self.definition, "librarian", {"name": "Rosa Vale"})
        self.assertNotEqual(first.id, second.id)

    def test_a_change_keeps_the_fields_it_did_not_touch(self):
        keeper = self.store.create(self.definition, "librarian", {"name": "Rosa Vale"})
        book = self.store.create(self.definition, "book", {
            "title": "Tide Tables", "status": "On shelf", "keeper": keeper.id})
        changed = self.store.update(self.definition, "book", book.id, {"status": "On loan"})
        self.assertEqual(changed.fields["title"], "Tide Tables")
        self.assertEqual((changed.fields["status"], changed.revision), ("On loan", 2))

    def test_a_change_from_a_stale_revision_is_refused(self):
        keeper = self.store.create(self.definition, "librarian", {"name": "Rosa Vale"})
        book = self.store.create(self.definition, "book", {
            "title": "Tide Tables", "status": "On shelf", "keeper": keeper.id})
        self.store.update(self.definition, "book", book.id, {"status": "On loan"})
        with self.assertRaises(RecordConflict):
            self.store.update(self.definition, "book", book.id, {"status": "On shelf"},
                              expected_revision=book.revision)


class RecordsStayTest(ProductWithoutCodeFixture):
    def test_records_survive_a_restart(self):
        """What someone adds to their product is still there when they come back."""
        self.add_people_and_records()
        reopened = RecordStore(TENANT, PRODUCT, PRIMARY)
        self.assertEqual(len(reopened.all()["book"]), 2)
        self.assertEqual(reopened.get("book", self.tide.id).fields["title"], "Tide Tables")

    def test_one_products_records_are_not_another_products(self):
        self.add_people_and_records()
        other = RecordStore(TENANT, "a-different-product", PRIMARY)
        self.assertEqual(other.all(), {})

    def test_one_organizations_records_are_not_anothers(self):
        self.add_people_and_records()
        other = RecordStore("someone-else", PRODUCT, PRIMARY)
        self.assertEqual(other.all(), {})

    def test_a_visitors_demo_copy_is_not_the_organizations_records(self):
        self.add_people_and_records()
        visitor = RecordStore(TENANT, PRODUCT, "demo-instance-1")
        self.assertEqual(visitor.all(), {})
        visitor.create(self.definition, "librarian", {"name": "Someone Else"})
        self.assertEqual(len(self.store.all()["librarian"]), 2, "the organization's records are untouched")


class ScopeIsDerivedTest(ProductWithoutCodeFixture):
    """Which records a caller sees is worked out from the definition's own scope path."""

    def setUp(self):
        super().setUp()
        self.add_people_and_records()
        with db.get_connection() as connection:
            for scope_id, name, anchor in (("open-shelf", "Open shelf", self.tide.id),
                                           ("reserve", "Reserve", self.almanac.id)):
                connection.execute("insert into record_scopes values (?, ?, ?, ?, ?, '')",
                                   (TENANT, PRODUCT, PRIMARY, scope_id, name))
                connection.execute("insert into record_scope_anchors values (?, ?, ?, ?, ?)",
                                   (TENANT, PRODUCT, PRIMARY, scope_id, anchor))

    def lookup(self, *scope_ids: str):
        return self.package.lookup_factory(RecordGrant(frozenset(scope_ids), is_admin=False))

    def test_a_caller_sees_only_the_anchors_they_were_granted(self):
        visible = self.lookup("open-shelf")
        self.assertEqual([book.id for book in visible._all()["book"]], [self.tide.id])
        self.assertIsNone(visible.get("book", self.almanac.id))

    def test_a_related_record_follows_its_path_to_the_anchor(self):
        """A librarian is in scope through the books they keep, which is what the path says."""
        visible = self.lookup("open-shelf")
        self.assertEqual([person.id for person in visible._all()["librarian"]], [self.rosa.id])
        self.assertEqual(visible.people("Otto", 5).matches, ())

    def test_a_caller_granted_both_sees_both(self):
        visible = self.lookup("open-shelf", "reserve")
        self.assertEqual(visible.count("book"), 2)
        self.assertEqual(visible.count("librarian"), 2)

    def test_the_conversation_never_mentions_a_record_outside_the_scope(self):
        turn = self.say("how many books are there", RecordGrant(frozenset({"open-shelf"}), is_admin=False))
        self.assertIn("Tide Tables", turn.reply.speech)
        self.assertNotIn("Winter Almanac", turn.reply.speech)


if __name__ == "__main__":
    unittest.main()
