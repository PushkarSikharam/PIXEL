"""Operator backup and restore check (5c plan, section 11.2, step 1)."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import db, ops  # noqa: E402
from test_engine_cutover import EngineCutoverFixture  # noqa: E402


class BackupTest(EngineCutoverFixture):
    def run_ops(self, *argv: str) -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = ops.main(list(argv))
        return code, json.loads(output.getvalue())

    def test_a_backup_is_taken_and_proven_restorable(self):
        self.turn("Show me the issues", session="before-backup", turn_id=1)
        with tempfile.TemporaryDirectory() as directory:
            code, report = self.run_ops("backup", "--to", directory)
            self.assertEqual(code, 0, report)
            self.assertTrue(report["restorable"])
            self.assertEqual(report["integrity"], "ok")
            self.assertTrue(report["application_starts_on_copy"])
            self.assertGreater(report["rows"]["messages"], 0)
            self.assertTrue(Path(report["backup"]).is_file())

    def test_verification_never_touches_the_live_database(self):
        with tempfile.TemporaryDirectory() as directory:
            _, report = self.run_ops("backup", "--to", directory)
            live_before = db.DB_PATH.stat().st_mtime_ns
            code, again = self.run_ops("verify-backup", "--path", report["backup"])
            self.assertEqual(code, 0)
            self.assertEqual(db.DB_PATH.stat().st_mtime_ns, live_before)
            self.assertEqual(again["sha256"], report["sha256"])

    def test_a_damaged_copy_is_not_restorable(self):
        with tempfile.TemporaryDirectory() as directory:
            broken = Path(directory) / "broken.sqlite3"
            broken.write_bytes(b"not a database" * 100)
            output = io.StringIO()
            with redirect_stdout(output):
                try:
                    code = ops.main(["verify-backup", "--path", str(broken)])
                except Exception as error:  # a crash would be a failure to report, not a pass
                    self.fail(f"verify-backup crashed: {error!r}")
            self.assertEqual(code, 1)
            self.assertFalse(json.loads(output.getvalue())["restorable"])

    def test_a_missing_copy_is_not_restorable(self):
        code, report = self.run_ops("verify-backup", "--path", "does-not-exist.sqlite3")
        self.assertEqual(code, 1)
        self.assertFalse(report["restorable"])


if __name__ == "__main__":
    unittest.main()
