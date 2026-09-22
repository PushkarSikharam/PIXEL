"""Browser-test entry point: the new engine answers `/api/turn`, with execution keys (5b plan, 11).

Production never imports this module: `Dockerfile.api` starts `app.main`, and a test proves that
nothing reachable from `app.main` imports it. It also refuses to load without an explicit test
marker, so starting it by mistake fails closed instead of serving the new engine.

    PIXEL_TESTING_ENTRY=browser-tests uvicorn app.testing_main:app
"""
from __future__ import annotations

import os

MARKER = "PIXEL_TESTING_ENTRY"
MARKER_VALUE = "browser-tests"

if os.environ.get(MARKER) != MARKER_VALUE:
    raise RuntimeError(f"app.testing_main serves only browser tests; set {MARKER}={MARKER_VALUE}")

from app.installed_products import package_for  # noqa: E402
from app.main import agent, app, turn_engine  # noqa: E402
from app.services.turn_execution import NewEngineTurns  # noqa: E402

new_engine_turns = NewEngineTurns(agent.sessions, agent.directory, package_for)
app.dependency_overrides[turn_engine] = lambda: new_engine_turns

__all__ = ["app"]
