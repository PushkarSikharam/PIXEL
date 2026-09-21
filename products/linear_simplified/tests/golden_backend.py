"""Records what the backend conversation engine decides for the golden conversations.

Regenerate the baseline (only with a justified behaviour change):
    python products/linear_simplified/tests/golden_backend.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

from app import db  # noqa: E402
from app.auth import AuthUser  # noqa: E402
from app.schemas import TurnRequest, TurnResponse  # noqa: E402
from app.services import env as env_module  # noqa: E402
from app.services.agent import DemoAgent  # noqa: E402
from app.services.product_data_store import ProductDataStore  # noqa: E402

# The seeded development organization's product running this definition, used by its admin.
PRODUCT_ID = "linear-demo"
PRINCIPAL = AuthUser(kind="member", user_id="demo-admin", tenant_id="pixel-dev", role="org_admin")
DEFAULT_WORKSPACE = "workspace-product-eng"
RECORDING = GOLDEN_DIR / "backend_decisions.json"
HERMETIC_ENV = {
    "LLM_ENABLED": "false",
    "PIXEL_PAID_PROVIDERS_ENABLED": "false",
    "PIXEL_BLOCK_EXTERNAL_HTTP": "true",
    "PIXEL_IGNORE_ENV_FILES": "true",
    "PIXEL_DEMO_SEEDS": "true",
}
# The page the browser shows after an action, so follow-up turns carry realistic context.
_PAGE_BY_ACTION = {
    "OPEN_DASHBOARD": "dashboard",
    "OPEN_ISSUES": "issues",
    "OPEN_PROJECTS": "projects",
    "OPEN_CYCLES": "cycles",
    "OPEN_TEAMS": "teams",
    "OPEN_INTEGRATIONS": "integrations",
    "FILTER_ISSUES_BY_ASSIGNEE": "issues",
    "HIGHLIGHT_CREATE_TICKET_BUTTON": "issues",
    "HIGHLIGHT_ADD_MEMBER_BUTTON": "teams",
    "HIGHLIGHT_CYCLE_PROGRESS": "cycles",
    "OPEN_GITHUB_SETUP": "integrations",
    "HIGHLIGHT_GITHUB_CARD": "integrations",
    "HIGHLIGHT_SLACK_CARD": "integrations",
}
_RECORD_ACTIONS = {"OPEN_DEMO_ISSUE", "UPDATE_DEMO_ISSUE", "HIGHLIGHT_ASSIGNMENT_CONTROL"}


def load_cases() -> list[dict]:
    return json.loads((GOLDEN_DIR / "conversations.json").read_text(encoding="utf-8"))["cases"]


def record_backend_decisions() -> dict[str, list[dict]]:
    with tempfile.TemporaryDirectory() as directory, \
            patch.dict(os.environ, HERMETIC_ENV), \
            patch.object(env_module, "_env_files", lambda: ()), \
            patch.object(db, "DB_PATH", Path(directory) / "golden.sqlite3"):
        db.migrate()
        agent = DemoAgent()
        return {case["id"]: _run_case(agent, case) for case in load_cases()}


def _run_case(agent: DemoAgent, case: dict) -> list[dict]:
    ProductDataStore().reset()
    page, selected = "dashboard", None
    turns = []
    for turn_id, message in enumerate(case["turns"], start=1):
        response = agent.handle_turn(TurnRequest(
            session_id=f"golden-{case['id']}",
            turn_id=turn_id,
            product_id=PRODUCT_ID,
            message=message,
            current_page=page,
            selected_issue_id=selected,
            workspace_scope_id=case.get("workspace", DEFAULT_WORKSPACE),
        ), PRINCIPAL, ProductDataStore().load())  # what the turn endpoint passes an administrator
        turns.append(_summarize(message, response))
        page, selected = _next_ui_state(response, page, selected)
    return turns


def _summarize(message: str, response: TurnResponse) -> dict:
    def action(value):
        return {"type": value.type, "payload": value.payload} if value else None

    return {
        "message": message,
        "status": response.status,
        "speech": response.speech,
        "proposed_action": action(response.proposed_action),
        "validated_action": action(response.validated_action),
        "relevant_feature": response.intent_trace.relevant_feature,
        "clarification_pending": response.session_summary.clarification_pending,
        "sources": [context.source for context in response.retrieved_context],
    }


def _next_ui_state(response: TurnResponse, page: str, selected: str | None) -> tuple[str, str | None]:
    action = response.validated_action
    if action is None:
        return page, selected
    if action.type in _RECORD_ACTIONS:
        return "issue_detail", action.payload.get("issue_id") or selected
    if action.type == "CREATE_DEMO_ISSUE":
        return "issue_detail", action.payload.get("id")
    return _PAGE_BY_ACTION.get(action.type, page), selected


def write_recording() -> None:
    RECORDING.write_text(json.dumps(record_backend_decisions(), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_recording()
    print(f"Wrote {RECORDING}")
