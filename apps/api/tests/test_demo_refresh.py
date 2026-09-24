"""The shared synthetic demo is restored between visitors.

The live reproduction: one visitor followed the guided path and assigned Maya's ticket to Noah.
Every later visitor then asked for "Maya's ticket" and was told there was none, until an operator
reset the data. A new visitor now starts from the seed when earlier visitors changed it and left.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

import app.main as main_module
from app import db
from app.main import app
from app.services.demo_refresh import IdleDemoReset
from test_public_demo import PublicDemoFixture

IDLE_MINUTES = 20


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += minutes * 60


class IdleDemoResetTest(PublicDemoFixture):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.reset = IdleDemoReset(clock=self.clock)
        with patch.dict(os.environ, {"PIXEL_DEMO_IDLE_RESET_MINUTES": str(IDLE_MINUTES)}):
            self.reset.arm()
        patcher = patch.object(main_module, "idle_reset", self.reset)
        patcher.start()
        self.addCleanup(patcher.stop)
        # The process starts not knowing whether the file holds earlier edits; begin known-clean.
        self.reset.restored()

    def maya_ticket_owner(self, headers) -> str:
        data = self.client.get("/api/demo-data", headers=headers).json()
        return next(issue["assignee"] for issue in data["issues"] if issue["id"] == "LIN-142")

    def reassign_to_noah(self, headers) -> None:
        data = self.client.get("/api/demo-data", headers=headers).json()
        issue = next(issue for issue in data["issues"] if issue["id"] == "LIN-142")
        response = self.client.put("/api/demo-data/issues/LIN-142", headers=headers,
                                   json={**issue, "assignee": "Noah Patel"})
        self.assertEqual(response.status_code, 200, response.text)

    def test_the_next_visitor_after_an_idle_gap_starts_from_the_seed(self):
        """The reproduction, end to end."""
        first = self.token("demo-visitor")
        self.assertEqual(self.maya_ticket_owner(first), "Maya Chen")
        self.reassign_to_noah(first)
        self.assertEqual(self.maya_ticket_owner(first), "Noah Patel")

        self.clock.advance(IDLE_MINUTES + 1)
        second = self.token("demo-visitor")

        self.assertEqual(self.maya_ticket_owner(second), "Maya Chen")

    def test_a_visitor_arriving_while_others_are_active_sees_their_changes(self):
        """Never pull data out from under someone who is still using the demo."""
        first = self.token("demo-visitor")
        self.reassign_to_noah(first)
        self.clock.advance(IDLE_MINUTES - 1)
        self.client.get("/api/demo-data", headers=first)  # still active
        self.clock.advance(IDLE_MINUTES - 1)

        second = self.token("demo-visitor")

        self.assertEqual(self.maya_ticket_owner(second), "Noah Patel")

    def test_unchanged_data_is_never_rewritten(self):
        self.token("demo-visitor")
        self.clock.advance(IDLE_MINUTES * 3)
        with patch.object(main_module.product_data, "reset") as restore:
            self.token("demo-visitor")
        restore.assert_not_called()

    def test_a_refused_sign_in_restores_nothing(self):
        first = self.token("demo-visitor")
        self.reassign_to_noah(first)
        self.clock.advance(IDLE_MINUTES + 1)
        self.assertEqual(self.login("demo-admin").status_code, 404)
        self.assertEqual(self.maya_ticket_owner(first), "Noah Patel")

    def test_an_administrator_reset_counts_as_restored(self):
        first = self.token("demo-visitor")
        self.reassign_to_noah(first)
        self.reset.restored()
        self.clock.advance(IDLE_MINUTES + 1)
        with patch.object(main_module.product_data, "reset") as restore:
            self.token("demo-visitor")
        restore.assert_not_called()

    def test_every_shared_record_write_marks_the_demo_changed(self):
        headers = self.token("demo-visitor")
        data = self.client.get("/api/demo-data", headers=headers).json()
        writes = (
            ("post", "/api/demo-data/issues", data["issues"][0]),
            ("put", f"/api/demo-data/issues/{data['issues'][0]['id']}", data["issues"][0]),
            ("post", "/api/demo-data/projects?workspace_scope_id=workspace-product-eng",
             data["projects"][0]),
            ("post", "/api/demo-data/cycles", data["cycles"][0]),
            ("post", "/api/demo-data/team-members?workspace_scope_id=workspace-product-eng",
             data["team"][0]),
        )
        for method, path, payload in writes:
            with self.subTest(path=path):
                self.reset.restored()
                self.client.request(method, path, headers=headers, json=payload)
                self.clock.advance(IDLE_MINUTES + 1)
                with patch.object(main_module.product_data, "reset") as restore:
                    self.token("demo-visitor")
                restore.assert_called_once()

    def test_the_restore_is_logged_for_operators(self):
        first = self.token("demo-visitor")
        self.reassign_to_noah(first)
        self.clock.advance(IDLE_MINUTES + 1)
        with self.assertLogs("pixel.demo", level="INFO") as logs:
            self.token("demo-visitor")
        self.assertIn('"event": "demo_data_restored"', logs.output[0])


class ArmingTest(unittest.TestCase):
    def test_off_unless_configured(self):
        for value in (None, "0", "-5", "soon"):
            with self.subTest(value=value):
                env = {} if value is None else {"PIXEL_DEMO_IDLE_RESET_MINUTES": value}
                with patch.dict(os.environ, env, clear=False):
                    if value is None:
                        os.environ.pop("PIXEL_DEMO_IDLE_RESET_MINUTES", None)
                    reset = IdleDemoReset()
                    reset.arm()
                    self.assertFalse(reset.armed)
                    restore = unittest.mock.Mock()
                    self.assertFalse(reset.before_sign_in(restore))
                    restore.assert_not_called()

    def test_a_process_that_just_started_assumes_the_data_changed(self):
        """After a restart nobody knows what the file holds; one restore is the safe answer."""
        clock = Clock()
        reset = IdleDemoReset(clock=clock)
        with patch.dict(os.environ, {"PIXEL_DEMO_IDLE_RESET_MINUTES": "20"}):
            reset.arm()
        clock.advance(21)
        restore = unittest.mock.Mock()
        self.assertTrue(reset.before_sign_in(restore))
        restore.assert_called_once()

    def test_a_running_server_arms_it_and_a_test_server_leaves_it_as_found(self):
        reset = IdleDemoReset()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        with patch.object(main_module, "idle_reset", reset), \
                patch.object(db, "DB_PATH", Path(temporary.name) / "demo-refresh.sqlite3"), \
                patch.dict(os.environ, {"PIXEL_DEMO_IDLE_RESET_MINUTES": "20"}):
            with TestClient(app):
                self.assertTrue(reset.armed)
            self.assertFalse(reset.armed)


if __name__ == "__main__":
    unittest.main()
