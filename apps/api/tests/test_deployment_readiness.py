"""Deployment readiness must cover the API process and its persistent storage."""
from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app import db
from app.main import app


class DeploymentReadinessTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        db_patch = patch.object(db, "DB_PATH", Path(temporary.name) / "readiness.sqlite3")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        db.migrate()
        self.client = TestClient(app)

    def test_root_and_proxied_health_routes_check_storage(self) -> None:
        for path in ("/health", "/api/health"):
            with self.subTest(path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"status": "ok", "authority": "legacy", "shadow": "off"})

    def test_health_fails_when_storage_is_unavailable(self) -> None:
        def unavailable():
            raise OSError("storage unavailable")

        with patch("app.main.get_connection", unavailable):
            response = TestClient(app, raise_server_exceptions=False).get("/api/health")
        self.assertEqual(response.status_code, 500)


if __name__ == "__main__":
    unittest.main()
