"""Offline shadow parity: every golden conversation through the live engine and the shadow (5a).

Each turn runs exactly as the turn endpoint runs it with `PIXEL_SHADOW_ENGINE=on`: the shadow
converts the caller's records before the live turn, the live engine answers, and the shadow
compares its own turn with the final live response. Every field of every turn gets one class.

Differences are evidence, never targets. Each one is listed in `golden/shadow_differences.json`
with its class and reason; the test fails on a difference that is not listed and on a listed one
that no longer occurs. (`golden/reviewed_differences.json` is the live engine's own list against
its recording, with its own kinds, and is not mixed with this one.)

    python -m products.linear_simplified.tests.golden_shadow   # print every non-matching field
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
import tempfile
from unittest.mock import patch

from products.linear_simplified.tests.golden_backend import (
    DEFAULT_WORKSPACE,
    GOLDEN_DIR,
    HERMETIC_ENV,
    PRINCIPAL,
    PRODUCT_ID,
    _next_ui_state,
    load_cases,
)
from app import db
from app.auth import product_record_grant
from app.installed_products import package_for
from app.main import live_turn
from app.schemas import TurnRequest
from app.services import env as env_module
from app.services.agent import DemoAgent
from app.services.product_data_store import ProductDataStore
from app.services.shadow import ShadowRunner
from app.services.shadow_parity import BEHAVIOUR, CLASSES, MATCH, SECURITY

SHADOW_DIFFERENCES = GOLDEN_DIR / "shadow_differences.json"
ENTRY_KEYS = frozenset({"case", "turn", "field", "kind", "reason"})


@dataclass(frozen=True)
class FieldClass:
    case: str
    turn: int
    message: str
    field: str
    cls: str


@dataclass(frozen=True)
class ShadowRun:
    turn_classes: tuple[tuple[str, int, str], ...]
    fields: tuple[FieldClass, ...]

    @property
    def differences(self) -> tuple[FieldClass, ...]:
        return tuple(item for item in self.fields if item.cls != MATCH)


def run_shadow_parity(env: dict[str, str] | None = None) -> ShadowRun:
    with tempfile.TemporaryDirectory() as directory, \
            patch.dict(os.environ, {**HERMETIC_ENV, **(env or {})}), \
            patch.object(env_module, "_env_files", lambda: ()), \
            patch.object(db, "DB_PATH", Path(directory) / "shadow.sqlite3"):
        db.migrate()
        agent = DemoAgent()
        runner = ShadowRunner(agent.directory, agent.sessions.pin_for, package_for)
        turn_classes: list[tuple[str, int, str]] = []
        fields: list[FieldClass] = []
        for case in load_cases():
            _run_case(agent, runner, case, turn_classes, fields)
        return ShadowRun(tuple(turn_classes), tuple(fields))


def _run_case(agent: DemoAgent, runner: ShadowRunner, case: dict,
              turn_classes: list, fields: list) -> None:
    ProductDataStore().reset()
    page, selected = "dashboard", None
    grant = product_record_grant(PRINCIPAL, PRODUCT_ID)
    for index, message in enumerate(case["turns"]):
        request = TurnRequest(
            session_id=f"golden-{case['id']}",
            turn_id=index + 1,
            product_id=PRODUCT_ID,
            message=message,
            current_page=page,
            selected_issue_id=selected,
            workspace_scope_id=case.get("workspace", DEFAULT_WORKSPACE),
        )
        visible = ProductDataStore().load(grant.visible_scope_ids())
        prepared = runner.prepare(PRINCIPAL, grant, PRODUCT_ID, visible, request.workspace_scope_id)
        # As the endpoint does: the live engine sees only the selected workspace (5c plan, 3.3).
        selected_data = ProductDataStore().load(frozenset({request.workspace_scope_id}))
        response = live_turn(request, PRINCIPAL, selected_data, grant)
        result = runner.complete(prepared, PRINCIPAL, request, response)
        turn_classes.append((case["id"], index, result.turn_class))
        for name, cls in result.classes.items():
            fields.append(FieldClass(case["id"], index, message, name, cls))
        page, selected = _next_ui_state(response, page, selected)


def _kind_fits(listed: str, found: str) -> bool:
    """A listed kind must be the class found, except that a reviewer may mark a behaviour
    difference as a deliberate security change; the comparator cannot know intent."""
    return listed == found or (listed == SECURITY and found == BEHAVIOUR)


def load_listed(path: Path = SHADOW_DIFFERENCES) -> list[dict]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    seen: set[tuple[str, int, str]] = set()
    for index, entry in enumerate(entries):
        if set(entry) != ENTRY_KEYS:
            raise ValueError(f"entry {index} must have exactly the keys {sorted(ENTRY_KEYS)}")
        if entry["kind"] not in CLASSES or entry["kind"] == MATCH:
            raise ValueError(f"entry {index} has kind {entry['kind']!r}, which is not a difference class")
        if len(entry["reason"].strip()) < 40:
            raise ValueError(f"entry {index} needs a concrete reason")
        location = (entry["case"], entry["turn"], entry["field"])
        if location in seen:
            raise ValueError(f"entry {index} repeats {location}")
        seen.add(location)
    return entries


def parity_report(run: ShadowRun, listed: list[dict]) -> dict[str, list]:
    found = {(d.case, d.turn, d.field): d for d in run.differences}
    by_location = {(e["case"], e["turn"], e["field"]): e for e in listed}
    return {
        "unlisted": [d for location, d in found.items() if location not in by_location],
        "stale": [e for location, e in by_location.items() if location not in found],
        "wrong_kind": [
            (e, found[location].cls) for location, e in by_location.items()
            if location in found and not _kind_fits(e["kind"], found[location].cls)
        ],
    }


if __name__ == "__main__":
    shadow_run = run_shadow_parity()
    for case, turn, cls in shadow_run.turn_classes:
        if cls != "compared":
            print(f"TURN {case}[{turn}] {cls}")
    for item in shadow_run.differences:
        print(f"{item.cls:17} {item.case}[{item.turn}].{item.field}  {item.message!r}")
