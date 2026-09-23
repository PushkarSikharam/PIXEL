"""The acceptance report that proves the 5c gates (5d plan, section 2.1a, revision 6).

The report is read-only evidence for a decision nobody should make from memory: whether definition
authority has covered every workflow, across enough days, without failures, to delete the engine
it replaced. Pixel has no visitors, so the gate is coverage rather than volume from strangers, and
the report is required to keep saying which sessions were run by the operator.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_engine_cutover import EngineCutoverFixture  # noqa: E402

from app import db, ops  # noqa: E402
from app.definitions.vocabulary import Capability  # noqa: E402
from app.services import turn_telemetry  # noqa: E402

PRODUCT = "linear-demo"


class AcceptanceReportTest(EngineCutoverFixture):
    def report(self, *argv: str) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = ops.main(["acceptance-report", *argv])
        return code, json.loads(output.getvalue())

    def add_session(self, session_id: str, turns: int, *, day: int = 0) -> None:
        started = datetime.now(UTC) - timedelta(days=day)
        with db.get_connection() as connection:
            connection.execute("insert into sessions(id, product_id, started_at) values (?, ?, ?)",
                               (session_id, PRODUCT, started.isoformat()))
            for turn in range(1, turns + 1):
                connection.execute(
                    "insert into messages(id, session_id, turn_id, role, content, created_at) "
                    "values (?, ?, ?, 'user', 'x', ?)",
                    (f"{session_id}-{turn}", session_id, turn, (started + timedelta(minutes=turn)).isoformat()),
                )

    def carry_out(self, capability: Capability, session_id: str = "real-1") -> None:
        """One executed action, which is how a workflow earns its evidence."""
        with db.get_connection() as connection:
            connection.execute(
                "insert into action_executions(execution_key, tenant_id, product_id, session_id, "
                "turn_id, user_id, action_key, capability, request_digest, state, result_code, "
                "created_at, expires_at, settled_at) values (?, 'pixel-dev', ?, ?, 1, "
                "'demo-admin', ?, ?, 'digest', 'executed', 'applied', ?, ?, ?)",
                (f"key-{capability}-{session_id}", PRODUCT, session_id,
                 f"do_{str(capability).lower()}", str(capability),
                 datetime.now(UTC).isoformat(), (datetime.now(UTC) + timedelta(hours=1)).timestamp(),
                 datetime.now(UTC).isoformat()),
            )

    def cover_every_workflow(self, session_id: str = "real-1") -> None:
        for capability in (Capability.NAVIGATE_VIEW, Capability.OPEN_RECORD,
                           Capability.FILTER_RECORDS, Capability.HIGHLIGHT_CONTROL,
                           Capability.CREATE_RECORD, Capability.UPDATE_RECORD):
            self.carry_out(capability, session_id)
        for outcome in ("denied", "stale"):
            turn_telemetry.record(deployment_id="test", tenant_id="pixel-dev", product_id=PRODUCT,
                                  definition_version=6, authority="definition",
                                  counts={"status": outcome})

    def record_turns(self, accepted: int, *, stale: int = 0) -> None:
        for _ in range(accepted):
            turn_telemetry.record(deployment_id="test", tenant_id="pixel-dev", product_id=PRODUCT,
                                  definition_version=5, authority="definition",
                                  counts={"status": "completed", "action": "open_issues"})
        for _ in range(stale):
            turn_telemetry.record(deployment_id="test", tenant_id="pixel-dev", product_id=PRODUCT,
                                  definition_version=5, authority="definition", counts={"status": "stale"})

    def test_a_window_that_has_not_earned_sign_off_says_which_gate_failed(self):
        self.add_session("real-1", 3)
        self.add_session("synthetic-visitor-check-a", 10)
        self.record_turns(13)
        code, report = self.report("--since", "2000-01-01", "--product", PRODUCT)
        self.assertEqual(code, 1, "an unmet gate exits non-zero")
        self.assertFalse(report["ready"])
        unmet = {gate["gate"] for gate in report["gates"] if not gate["met"]}
        self.assertIn("every workflow of the matrix has evidence", unmet)
        self.assertIn("days covered", unmet)
        self.assertIn("a restart happened while conversations were open", unmet)

    def test_a_workflow_with_no_evidence_is_named(self):
        """The gate that replaced volume: no workflow may pass on memory alone."""
        self.add_session("real-1", 4)
        self.record_turns(4)
        self.carry_out(Capability.NAVIGATE_VIEW)
        self.assertEqual(self.report("--since", "2000-01-01", "--product", PRODUCT)[0], 1)
        _, report = self.report("--since", "2000-01-01", "--product", PRODUCT)
        self.assertIn("navigation", report["workflows"]["covered"])
        self.assertIn("create a record", report["workflows"]["missing"])
        self.assertIn("refuse a request outside the product", report["workflows"]["missing"])

    def test_the_report_never_calls_an_operator_session_a_real_visitor(self):
        self.add_session("synthetic-visitor-check-a", 5)
        self.record_turns(5)
        _, report = self.report("--since", "2000-01-01", "--product", PRODUCT)
        self.assertEqual((report["sessions"]["real"], report["sessions"]["synthetic"]), (0, 1))
        self.assertIn("synthetic", report["note"])
        self.assertNotIn("real visitor", json.dumps(report["gates"]))

    def test_real_and_synthetic_sessions_are_counted_apart(self):
        self.add_session("visitor-1", 4)
        self.add_session("synthetic-visitor-check-a", 6)
        self.record_turns(10)
        _, report = self.report("--since", "2000-01-01", "--product", PRODUCT)
        self.assertEqual(report["sessions"], {"total": 2, "real": 1, "synthetic": 1})
        self.assertEqual((report["turns"]["real"], report["turns"]["synthetic"]), (4, 6))

    def test_a_restart_counts_only_sessions_that_were_open_across_it(self):
        self.add_session("open-across", 6)
        middle = self.middle_message()  # a moment inside the session's own span
        _, report = self.report("--since", "2000-01-01", "--product", PRODUCT,
                                "--deploy-at", middle, "--deploy-at", "2100-01-01T00:00:00")
        self.assertEqual([entry["sessions_open"] for entry in report["restarts"]], [1, 0])

    def test_every_gate_met_is_reported_ready(self):
        for index in range(4):
            self.add_session(f"synthetic-visitor-check-{index}", 10, day=index)
        self.record_turns(40)
        self.cover_every_workflow("synthetic-visitor-check-0")
        code, report = self.report("--since", "2000-01-01", "--product", PRODUCT,
                                   "--deploy-at", self.middle_message())
        unmet = [gate for gate in report["gates"] if not gate["met"]]
        self.assertEqual(unmet, [], report["gates"])
        self.assertTrue(report["ready"])
        self.assertEqual(code, 0)
        self.assertIn("open_issues", report["workflows"]["actions_seen"])

    def test_the_report_writes_nothing(self):
        self.add_session("real-1", 2)
        before = self.row_counts()
        self.report("--since", "2000-01-01")
        self.assertEqual(self.row_counts(), before)

    @staticmethod
    def middle_message() -> str:
        """A stored turn timestamp with a session's turns on both sides of it."""
        with db.get_connection() as connection:
            rows = [row["created_at"] for row in connection.execute(
                "select created_at from messages order by created_at")]
        return rows[len(rows) // 2]

    @staticmethod
    def row_counts() -> tuple[int, ...]:
        with db.get_connection() as connection:
            return tuple(
                connection.execute(f"select count(*) from {table}").fetchone()[0]
                for table in ("sessions", "messages", "action_executions", "turn_telemetry_daily")
            )


if __name__ == "__main__":
    unittest.main()
