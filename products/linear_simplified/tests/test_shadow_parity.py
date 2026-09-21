"""Slice 5a: offline shadow parity test (plan section 5.2 and 5.3)."""
from __future__ import annotations

import unittest

from products.linear_simplified.tests.golden_shadow import (
    load_listed,
    parity_report,
    run_shadow_parity,
)


class ShadowParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shadow_run = run_shadow_parity()
        cls.listed = load_listed()

    def test_every_turn_is_compared_or_over_budget(self):
        for case_id, turn_index, turn_class in self.shadow_run.turn_classes:
            with self.subTest(case=case_id, turn=turn_index):
                self.assertIn(
                    turn_class,
                    {"compared", "over_budget"},
                    f"turn {case_id}[{turn_index}] had turn class {turn_class}",
                )

    def test_shadow_parity_matches_reviewed_differences(self):
        report = parity_report(self.shadow_run, self.listed)
        self.assertEqual(
            report["unlisted"],
            [],
            f"Unlisted shadow differences found: {report['unlisted']}",
        )
        self.assertEqual(
            report["stale"],
            [],
            f"Stale shadow differences found: {report['stale']}",
        )
        self.assertEqual(
            report["wrong_kind"],
            [],
            f"Wrong kind shadow differences found: {report['wrong_kind']}",
        )
