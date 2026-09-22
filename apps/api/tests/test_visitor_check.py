"""The cutover visitor script (5c plan, section 11.3, step 3) passes against the app in both authorities.

The script runs against a deployed API over HTTP; here the same checks run in-process through the
test client, so a change that would fail the operator's cutover check fails CI first.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_engine_cutover import EngineCutoverFixture  # noqa: E402

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "visitor_check.py"
spec = importlib.util.spec_from_file_location("visitor_check", SCRIPT)
visitor_check = importlib.util.module_from_spec(spec)
sys.modules["visitor_check"] = visitor_check
assert spec.loader is not None
spec.loader.exec_module(visitor_check)


class VisitorCheckTest(EngineCutoverFixture):
    def transport(self):
        def send(method, path, body, headers):
            response = self.client.request(method, path, json=body, headers=headers)
            kind = response.headers.get("content-type", "")
            return response.status_code, response.json() if "json" in kind else response.content
        return send

    def run_check(self, authority: str) -> visitor_check.VisitorCheck:
        # Without a startup, readiness reads the authority from the environment, as selected here.
        with patch.dict(os.environ, {"PIXEL_ENGINE_MODE": "" if authority == "legacy" else authority}):
            check = visitor_check.VisitorCheck(self.transport(), authority)
            check.run()
        return check

    def assert_all_pass(self, check) -> None:
        failed = [f"{r.name}: {r.detail}" for r in check.results if not r.passed]
        self.assertEqual(failed, [])
        self.assertEqual(len(check.results), 11)

    def test_every_check_passes_under_legacy_authority(self):
        self.assert_all_pass(self.run_check("legacy"))

    def test_every_check_passes_under_definition_authority(self):
        self.assert_all_pass(self.run_check("definition"))

    def test_a_wrong_authority_fails_the_readiness_check(self):
        with patch.dict(os.environ, {"PIXEL_ENGINE_MODE": ""}):
            check = visitor_check.VisitorCheck(self.transport(), "definition")
            check.check_health("readiness")
        self.assertFalse(check.results[0].passed)


if __name__ == "__main__":
    unittest.main()
