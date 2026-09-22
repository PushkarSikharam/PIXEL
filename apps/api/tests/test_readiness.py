"""Readiness means a conversation can start (the 2026-09-18 production outage).

Production answered `/health` with `{"status": "ok"}` while refusing every conversation: its
database had registered the product definition from a Windows checkout, so the registered checksum
was of CRLF bytes while the container held the LF file. These tests rebuild that exact state and
prove the platform now reports it, instead of looking healthy while nothing works.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app import db
from app.auth import create_token
from app.definitions.integrity import ReadinessCheck, session_start_problems
from app.definitions.loader import DEFAULT_SOURCE
from app.main import app, readiness
from app.services import env as env_module

DEFINITION_ID = "linear_simplified"


def crlf_checksum() -> str:
    """The checksum a Windows checkout of the definition produces: the production failure."""
    raw = DEFAULT_SOURCE.definition_path(DEFINITION_ID, 1).read_bytes()
    return hashlib.sha256(raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")).hexdigest()


class ReadinessFixture(unittest.TestCase):
    def setUp(self):
        files_patch = patch.object(env_module, "_env_files", lambda: ())
        files_patch.start()
        self.addCleanup(files_patch.stop)
        env_patch = patch.dict(os.environ, {"LLM_ENABLED": "false", "PIXEL_SYNTHETIC_DEMO": "true",
                                            "PIXEL_DEMO_SEEDS": "true"})
        env_patch.start()
        self.addCleanup(env_patch.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        db_patch = patch.object(db, "DB_PATH", Path(temporary.name) / "readiness.sqlite3")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        db.migrate()
        readiness.invalidate()
        self.addCleanup(readiness.invalidate)
        self.client = TestClient(app)

    def register_windows_checksum(self) -> None:
        """Rebuild production's database: rows registered from CRLF bytes, file served as LF."""
        checksum = crlf_checksum()
        with db.get_connection() as connection:
            connection.execute("update definition_versions set checksum = ? where definition_id = ?",
                               (checksum, DEFINITION_ID))
            connection.execute("update product_bindings set definition_checksum = ? "
                               "where definition_id = ?", (checksum, DEFINITION_ID))
        readiness.invalidate()


class ReadinessTest(ReadinessFixture):
    def test_a_healthy_deployment_reports_ok(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "authority": "legacy", "shadow": "off"})

    def test_the_line_ending_mismatch_is_what_production_hit(self):
        """Guard the premise: the two checkouts really do give the file different identities."""
        lf = hashlib.sha256(DEFAULT_SOURCE.definition_path(DEFINITION_ID, 1).read_bytes()).hexdigest()
        self.assertNotEqual(lf, crlf_checksum())

    def test_a_definition_registered_from_other_bytes_makes_health_unhealthy(self):
        self.register_windows_checksum()
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(),
                         {"status": "unhealthy", "reason": "sessions_cannot_start", "products": 1})

    def test_health_and_a_real_turn_agree(self):
        """The check runs the same code a request runs, so they cannot disagree."""
        self.register_windows_checksum()
        problems = session_start_problems()
        self.assertEqual([problem.reason for problem in problems], ["definition_invalid"])

        token = create_token("demo-product-eng", "pixel-dev")
        turn = self.client.post("/api/turn", headers={"Authorization": f"Bearer {token}"}, json={
            "session_id": "readiness-check", "turn_id": 1, "product_id": "linear-demo",
            "message": "Show sprint planning", "input_mode": "text", "current_page": "dashboard",
            "workspace_scope_id": "workspace-product-eng",
        }).json()
        self.assertEqual(turn["status"], "denied")
        self.assertIn("definition_invalid", turn["intent_trace"]["reason"])

    def test_the_public_answer_names_no_tenant_product_or_definition(self):
        """Health is unauthenticated; on a multi-tenant platform it may not list anyone's products."""
        self.register_windows_checksum()
        body = self.client.get("/health").text
        for identifier in ("pixel-dev", "linear-demo", DEFINITION_ID, "definition_invalid"):
            self.assertNotIn(identifier, body)

    def test_the_details_go_to_the_operator_log(self):
        self.register_windows_checksum()
        with self.assertLogs("pixel.readiness", level="WARNING") as logged:
            self.client.get("/health")
        record = "\n".join(logged.output)
        self.assertIn("session_start_unavailable", record)
        self.assertIn("definition_invalid", record)
        self.assertIn("linear-demo", record)

    def test_a_disabled_product_does_not_make_the_deployment_unhealthy(self):
        """An operator switching a product off is a decision, not a fault."""
        from app.definitions.organizations import OrganizationDirectory

        self.register_windows_checksum()
        OrganizationDirectory().set_product_state("pixel-dev", "linear-demo", "disabled")
        readiness.invalidate()
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_the_api_prefixed_health_route_agrees(self):
        self.register_windows_checksum()
        self.assertEqual(self.client.get("/api/health").status_code, 503)


class ReadinessCacheTest(ReadinessFixture):
    def test_the_answer_is_reused_briefly_then_rechecked(self):
        clock = [100.0]
        check = ReadinessCheck(ttl_seconds=30, clock=lambda: clock[0])
        self.assertEqual(check.problems(), ())
        self.register_windows_checksum()
        self.assertEqual(check.problems(), (), "within the window, the cached answer stands")
        clock[0] += 31
        self.assertEqual(len(check.problems()), 1, "after the window, the change is seen")

    def test_a_different_database_never_reuses_the_answer(self):
        """Tests and restored backups swap databases; a stale answer must not cross over."""
        clock = [100.0]
        check = ReadinessCheck(ttl_seconds=3600, clock=lambda: clock[0])
        self.assertEqual(check.problems(), ())
        self.register_windows_checksum()

        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        with patch.object(db, "DB_PATH", Path(other.name) / "other.sqlite3"):
            db.migrate()
            with db.get_connection() as connection:
                connection.execute("update definition_versions set checksum = ?", (crlf_checksum(),))
                connection.execute("update product_bindings set definition_checksum = ?",
                                   (crlf_checksum(),))
            self.assertEqual(len(check.problems()), 1)


if __name__ == "__main__":
    unittest.main()
