"""The seeded demo records say the same thing however they are read.

The live reproduction: the dashboard showed "68% cycle progress" beside a cycle with 18 of 38
items done, and called a cycle that had ended a fortnight earlier "Active, 8 days left". Both
numbers were stored beside the work they described and nothing kept them in step with it.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.product_data_store import SEED_CYCLES, dated  # noqa: E402


class SeededCycleTest(unittest.TestCase):
    def test_progress_is_the_share_of_planned_work_that_is_done(self):
        for cycle in SEED_CYCLES:
            with self.subTest(cycle=cycle["id"]):
                planned = cycle["completed"] + cycle["inProgress"] + cycle["remaining"]
                self.assertGreater(planned, 0)
                self.assertEqual(cycle["progress"], round(cycle["completed"] * 100 / planned))

    def test_a_cycle_called_active_is_running_on_the_day_it_is_seeded(self):
        for anchor in (date(2026, 1, 1), date(2027, 6, 30), date.today()):
            for cycle in SEED_CYCLES:
                with self.subTest(cycle=cycle["id"], anchor=anchor):
                    seeded = dated(cycle, anchor)
                    started = date.fromisoformat(seeded["startDate"])
                    ends = date.fromisoformat(seeded["endDate"])
                    self.assertLess(started, ends)
                    if cycle["status"] == "Active":
                        self.assertLessEqual(started, anchor, "an active cycle has started")
                        self.assertGreater(ends, anchor, "an active cycle has not ended")
                        self.assertEqual(seeded["daysLeft"], (ends - anchor).days)


if __name__ == "__main__":
    unittest.main()
