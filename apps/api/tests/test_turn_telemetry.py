"""Milestone 3.2, Slice 5c: cutover telemetry (plan, section 10). Metadata only."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import db, ops  # noqa: E402
from app.services import turn_telemetry  # noqa: E402
from test_engine_cutover import HERMETIC, EngineCutoverFixture  # noqa: E402


class TurnTelemetryTest(EngineCutoverFixture):
    def rows(self) -> list[dict]:
        with db.get_connection() as connection:
            return [dict(row) for row in connection.execute("select * from turn_telemetry_daily")]

    def test_each_turn_counts_status_stage_dispatch_and_latency_per_authority(self):
        with patch.dict(os.environ, {**HERMETIC, "PIXEL_ENGINE_MODE": "legacy"}, clear=False):
            self.turn("Show me the issues", session="t-legacy", turn_id=1)
        with patch.dict(os.environ, {**HERMETIC, "PIXEL_ENGINE_MODE": "definition"}, clear=False):
            mutation = self.turn("assign LIN-142 to Noah", session="t-definition", turn_id=1).json()
            headers = {**self.headers, "X-Execution-Key": mutation["execution"]["key"], "X-Session-Id": "t-definition"}
            self.client.patch("/api/demo-data/issues/LIN-142", json={"changes": {"assignee": "Noah Patel"}},
                              headers=headers)
        counts = {(r["authority"], r["metric"], r["value"]): r["count"] for r in self.rows()}
        self.assertEqual(counts[("legacy", "status", "completed")], 1)
        self.assertEqual(counts[("legacy", "dispatch", "none")], 1)
        self.assertEqual(counts[("definition", "stage", "proposed")], 1)
        self.assertEqual(counts[("definition", "dispatch", "keyed")], 1)
        self.assertEqual(counts[("definition", "receipt", "executed")], 1)
        self.assertEqual(counts[("definition", "receipt_code", "applied")], 1)
        self.assertEqual(sum(v for (a, m, _), v in counts.items() if a == "legacy" and m == "latency"), 1)

    def test_nothing_identifying_or_spoken_is_stored(self):
        self.turn("assign LIN-142 to Noah", session="secret-session-id", turn_id=1)
        stored = json.dumps(self.rows())
        for forbidden in ("secret-session-id", "LIN-142", "Noah", "demo-admin", "assign"):
            self.assertNotIn(forbidden, stored)

    def test_the_report_gives_counts_and_rates_per_authority(self):
        self.turn("Show me the issues", session="r1", turn_id=1)
        self.turn("delete all issues", session="r2", turn_id=1)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(ops.main(["cutover-report", "--days", "1"]), 0)
        report = json.loads(output.getvalue())["authorities"]["legacy"]
        self.assertEqual(report["rates"]["turns"], 2)
        self.assertEqual(report["rates"]["denied"], 0.5)

    def test_rows_older_than_the_retention_window_are_pruned(self):
        self.turn("Show me the issues", session="p1", turn_id=1)
        old = (datetime.now(UTC) - timedelta(days=turn_telemetry.RETENTION_DAYS + 1)).date().isoformat()
        with db.get_connection() as connection:
            connection.execute("update turn_telemetry_daily set day = ?", (old,))
        self.assertGreater(turn_telemetry.prune(), 0)
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
