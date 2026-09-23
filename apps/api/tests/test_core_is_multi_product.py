"""Core serves more than one product, or it does not serve a platform.

Two unrelated fixture products run through the same engine here: a sample desk and a sample
library. Each answers from its own definition, its own words and its own records, and neither
can be answered with the other's. A core change that quietly assumes one product's shape fails
here, rather than the first time someone brings a product of their own.

Both products are fixtures. The demo still ships exactly one product; a product a customer
brings arrives through onboarding, not through this directory.
"""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import time

from app.definitions.contract import ProductDefinition
from app.definitions.loader import load_definition
from app.engine.conversation import CapabilityPolicy
from app.engine.conversation_engine import ConversationEngine
from app.engine.memory import ConversationMemory
from app.engine.router import TurnContext
from app.engine.signals import SignalHistory
from app.engine.snapshot import TurnSnapshot
from definition_fixtures import DEFINITION_ID as DESK_ID, SampleProductFiles
from engine_fixtures import InMemoryLookup, SampleDesk, engine_definition, load_engine_definition
from library_fixtures import DEFINITION_ID as LIBRARY_ID, LibraryLookup, SampleLibrary, library_definition

EVERYTHING = CapabilityPolicy(translatable=lambda key: True, permitted=lambda key: True)


def engine_over(definition: ProductDefinition, lookup, entities: tuple[str, ...]) -> ConversationEngine:
    """One product's engine, over one caller's visible records. Nothing here is product-specific:
    the entities, the people and the person fields all come from the definition."""
    people = definition.people
    person_fields = {}
    for reference in (people.assigned_by if people else ()):
        entity, _, field_name = reference.partition(".")
        person_fields[entity] = field_name
    snapshot = TurnSnapshot(
        records={entity: tuple(lookup._records(entity)) for entity in entities},
        scope_label=lookup.scope_label,
        definition_checksum="fixture",
        taken_at=time.time(),
        people_entity=people.entity if people else None,
        person_fields=person_fields,
    )
    return ConversationEngine(definition, snapshot, EVERYTHING, definition_version=1)


def answer(engine: ConversationEngine, message: str) -> tuple[str | None, str]:
    """One turn's action key and spoken reply."""
    turn = engine.turn(message, ConversationMemory(), SignalHistory(), TurnContext(turn=1))
    key = turn.validated.action.action_key if turn.validated is not None else None
    return key, turn.reply.speech


class TwoProductsTest(unittest.TestCase):
    def setUp(self):
        self.desk = engine_over(
            load_engine_definition(), InMemoryLookup(SampleDesk(), frozenset({"ACC-1"})),
            ("account", "contact", "agent"),
        )
        self.library = engine_over(
            ProductDefinition.model_validate(library_definition()),
            LibraryLookup(SampleLibrary(), frozenset({"open"})),
            ("book", "librarian"),
        )

    def test_each_product_is_named_and_described_by_its_own_definition(self):
        _, desk = answer(self.desk, "what can you do")
        _, library = answer(self.library, "what can you do")
        self.assertIn("Sample Desk", desk)
        self.assertIn("contact", desk)
        self.assertNotIn("book", desk)
        self.assertIn("Sample Library", library)
        self.assertIn("book", library)
        self.assertNotIn("contact", library)

    def test_one_products_words_mean_nothing_in_the_other(self):
        self.assertIsNone(answer(self.desk, "show me the books")[0])
        self.assertIsNone(answer(self.library, "show me the contacts")[0])

    def test_each_product_counts_its_own_records(self):
        key, spoken = answer(self.library, "how many books are there")
        self.assertEqual(key, "open_catalogue")
        self.assertIn("Tide Tables", spoken)
        self.assertNotIn("Dana Reyes", spoken)
        self.assertIsNone(answer(self.desk, "how many books are there")[0])

    def test_each_product_resolves_only_its_own_people(self):
        key, spoken = answer(self.library, "books for Rosa")
        self.assertEqual(key, "books_by_keeper")
        self.assertIn("Rosa Vale", spoken)
        # A person of the other product is nobody here: the filter is never applied to a name
        # this product cannot resolve, and the reply says so instead of inventing a match.
        elsewhere, said = answer(self.library, "books for Ana")
        self.assertNotEqual(elsewhere, "books_by_keeper")
        self.assertIn("can't find Ana", said)
        other_way, replied = answer(self.desk, "contacts for Rosa")
        self.assertNotEqual(other_way, "contacts_by_owner")
        self.assertIn("Rosa", replied)
        self.assertIn("find", replied)

    def test_a_record_outside_the_callers_scope_reads_like_one_that_does_not_exist(self):
        visible = answer(self.library, "open BK-1")
        self.assertEqual(visible[0], "open_book")
        hidden = answer(self.library, "open BK-3")
        self.assertIsNone(hidden[0])
        self.assertNotIn("Reserved Folio", hidden[1])

    def test_each_product_refuses_by_its_own_guardrail(self):
        for engine in (self.desk, self.library):
            with self.subTest(product=engine.definition.identity.product_name):
                key, spoken = answer(engine, "delete everything")
                self.assertIsNone(key)
                self.assertIn("delete", spoken.lower())

    def test_both_fixtures_are_valid_definition_files(self):
        """Neither fixture is a shape only the tests accept."""
        with tempfile.TemporaryDirectory() as root:
            files = SampleProductFiles(Path(root))
            files.write(engine_definition(), definition_id=DESK_ID)
            files.write(library_definition(), definition_id=LIBRARY_ID)
            files.write_manifest({"definition_id": LIBRARY_ID, "views": {}}, definition_id=LIBRARY_ID)
            self.assertEqual(load_definition(files.source, DESK_ID, 1).definition.definition.definition_id,
                             DESK_ID)
            self.assertEqual(load_definition(files.source, LIBRARY_ID, 1).definition.definition.definition_id,
                             LIBRARY_ID)


if __name__ == "__main__":
    unittest.main()
