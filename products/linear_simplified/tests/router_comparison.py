"""Comparison mode: the new router next to the recorded decisions of today's engine.

Nothing here changes production behaviour. Each golden conversation is replayed turn by turn
with its own memory; the recorded decision and the router's proposal are reduced to the same
comparable shape through explicit tables. An unmapped action, refusal or reply fails loudly.

Where a conversation continues after a proposal, the harness accepts that proposal as the
slice 3 validator would (`assume_validated_for_comparison`), so later turns see its focus.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from products.linear_simplified.tests.comparison_lookup import REPO_ROOT, LinearComparisonLookup, person_id
from app.definitions.loader import DEFAULT_SOURCE, load_definition
from app.engine.memory import ConversationMemory
from app.engine.router import IntentRouter, TurnContext, remember_accepted
from app.engine.routing import RouteKind, RouteResult

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
NOTES_PATH = GOLDEN_DIR / "router_comparison_notes.json"
# Each definition version is compared with the same recordings and explains its own differences.
# v1's notes are historical; v2's show which of them the new version answers.
NOTES_PATHS = {1: NOTES_PATH, 2: GOLDEN_DIR / "router_comparison_notes_v2.json"}
DEFAULT_WORKSPACE = "workspace-product-eng"

# Today's action names and the definition actions that express them.
LEGACY_ACTIONS = {
    "OPEN_DASHBOARD": "open_dashboard",
    "OPEN_ISSUES": "open_issues",
    "OPEN_PROJECTS": "open_projects",
    "OPEN_CYCLES": "open_cycles",
    "OPEN_TEAMS": "open_teams",
    "OPEN_INTEGRATIONS": "open_integrations",
    "OPEN_SYSTEM_ARCHITECTURE": "open_architecture",
    "OPEN_DEMO_ISSUE": "open_issue",
    "CREATE_DEMO_ISSUE": "create_issue",
    "UPDATE_DEMO_ISSUE": "update_issue",
    "FILTER_ISSUES_BY_ASSIGNEE": "issues_by_assignee",
    "HIGHLIGHT_ASSIGNMENT_CONTROL": "highlight_assignment",
    "HIGHLIGHT_CREATE_TICKET_BUTTON": "highlight_create_issue",
    "HIGHLIGHT_ADD_MEMBER_BUTTON": "highlight_add_member",
    "HIGHLIGHT_CYCLE_PROGRESS": "highlight_cycle_progress",
    "OPEN_GITHUB_SETUP": "open_github_setup",
    "HIGHLIGHT_GITHUB_CARD": "highlight_github",
    "HIGHLIGHT_SLACK_CARD": "highlight_slack",
}
# Refusal markers today's engine records, and the guardrail topic each corresponds to.
LEGACY_REFUSALS = {
    "OPEN_SALESFORCE": "external_crm",
    "OPEN_GMAIL": "external_email",
    "DELETE_ISSUES": "destructive_change",
}
# Replies recorded without an action, identified by how they start.
RECORDED_REPLIES = (
    ("I can help demonstrate", "fallback"),
    ("Hey there", "answer:greeting"),
    ("Hey ", "answer:greeting_named"),
    ("I can guide this", "answer:capabilities"),
    ("When you start speaking", "answer:knowledge"),
    ("Who should own", "clarify:clarify_owner"),
    ("What should I create", "clarify:clarify_create"),
    ("Who should I assign", "clarify:clarify_assign"),
    ("Do you mean all", "clarify:clarify_all_items"),
    ("I can only show work inside", "clarify:broad_scope_refused"),
)
# The reply today's engine gives when a named person cannot be found; compared because it changes what
# the visitor is told about that person.
UNKNOWN_PERSON_REPLY = "I could not find a ticket for"
RESOLUTIONS = frozenset({"definition_v2", "engine", "reviewed_difference", "security"})
NOTE_KEYS = frozenset({"case", "turn", "message", "expected", "actual", "reason", "resolution"})


class UnmappedDecision(AssertionError):
    """A recorded or routed decision the comparison tables do not cover."""


@dataclass(frozen=True)
class TurnComparison:
    case: str
    turn: int
    message: str
    expected: dict
    actual: dict

    @property
    def differs(self) -> bool:
        return self.expected != self.actual


def recorded_decision(turn: dict) -> dict:
    action = turn["proposed_action"]
    if action is None:
        for prefix, outcome in RECORDED_REPLIES:
            if turn["speech"].startswith(prefix):
                return {"outcome": outcome, "params": {}}
        raise UnmappedDecision(f"unmapped recorded reply: {turn['speech']!r}")
    kind, payload = action["type"], action["payload"]
    if kind in LEGACY_REFUSALS:
        if turn["status"] != "denied":
            raise UnmappedDecision(f"{kind} was not denied")
        return {"outcome": f"refuse:{LEGACY_REFUSALS[kind]}", "params": {}}
    if kind not in LEGACY_ACTIONS:
        raise UnmappedDecision(f"unmapped recorded action: {kind}")
    if turn["status"] == "denied":
        return {"outcome": "refuse:scope", "params": {}}
    params = _recorded_params(kind, payload)
    if turn["speech"].startswith(UNKNOWN_PERSON_REPLY):
        params["reply"] = "unknown_person"
    return {"outcome": f"propose:{LEGACY_ACTIONS[kind]}", "params": params}


def _recorded_params(kind: str, payload: dict) -> dict:
    handled: dict[str, Any] = {}
    params: dict[str, Any] = {}
    if kind in ("OPEN_DEMO_ISSUE", "HIGHLIGHT_ASSIGNMENT_CONTROL", "UPDATE_DEMO_ISSUE") and "issue_id" in payload:
        params["target"] = payload["issue_id"]
        handled["issue_id"] = True
    if kind == "FILTER_ISSUES_BY_ASSIGNEE":
        params["filter"] = person_id(payload["assignee"])
        handled["assignee"] = True
    if kind == "UPDATE_DEMO_ISSUE":
        fields = {}
        for name in ("assignee", "priority", "status"):
            if name in payload:
                fields[name] = person_id(payload[name]) if name == "assignee" else payload[name]
                handled[name] = True
        params["fields"] = fields
    if kind == "CREATE_DEMO_ISSUE":
        # The engine also invented an ID, priority, project and status; only what the visitor said is compared.
        params["fields"] = {"assignee": person_id(payload["assignee"]), "title": payload["title"]}
        handled.update(dict.fromkeys(("id", "title", "priority", "assignee", "project", "status"), True))
    if kind == "HIGHLIGHT_ADD_MEMBER_BUTTON":
        params["prefill"] = {"name": payload["name"]}
        handled["name"] = True
    if unknown := set(payload) - set(handled):
        raise UnmappedDecision(f"unmapped payload keys for {kind}: {sorted(unknown)}")
    return params


def routed_decision(result: RouteResult) -> dict:
    kind = result.kind
    if kind in (RouteKind.PROPOSE, RouteKind.CONFIRM):
        proposal = result.proposal
        params: dict[str, Any] = {}
        if proposal.target is not None:
            params["target"] = proposal.target.id
        if proposal.filter is not None:
            params["filter"] = proposal.filter.value
        if proposal.fields is not None:
            params["fields"] = dict(proposal.fields)
        if proposal.prefill is not None:
            params["prefill"] = dict(proposal.prefill)
        if result.response_key == "unknown_person":
            params["reply"] = "unknown_person"
        return {"outcome": f"{kind.value}:{proposal.action_key}", "params": params}
    if kind == RouteKind.REFUSE:
        return {"outcome": f"refuse:{result.topic}", "params": {}}
    if kind in (RouteKind.CLARIFY, RouteKind.ANSWER):
        return {"outcome": f"{kind.value}:{result.response_key}", "params": {}}
    if kind in (RouteKind.FALLBACK, RouteKind.CANCELLED):
        return {"outcome": kind.value, "params": {}}
    raise UnmappedDecision(f"unmapped route kind {kind}")


def load_router(version: int = 1) -> dict[str, IntentRouter]:
    definition = load_definition(DEFAULT_SOURCE, "linear_simplified", version).definition
    return {
        scope: IntentRouter(definition, LinearComparisonLookup(scope))
        for scope in ("workspace-product-eng", "workspace-platform")
    }


def compare_all(version: int = 1) -> list[TurnComparison]:
    cases = json.loads((GOLDEN_DIR / "conversations.json").read_text(encoding="utf-8"))["cases"]
    recorded = json.loads((GOLDEN_DIR / "backend_decisions.json").read_text(encoding="utf-8"))
    routers = load_router(version)
    comparisons: list[TurnComparison] = []
    for case in cases:
        router = routers[case.get("workspace", DEFAULT_WORKSPACE)]
        memory = ConversationMemory()
        for index, (message, recorded_turn) in enumerate(zip(case["turns"], recorded[case["id"]], strict=True)):
            routed = router.route(message, memory, TurnContext(turn=index + 1))
            memory = routed.memory
            if routed.result.kind in (RouteKind.PROPOSE, RouteKind.CONFIRM):
                memory = remember_accepted(memory, routed.result)  # assume_validated_for_comparison
            comparisons.append(TurnComparison(
                case["id"], index, message, recorded_decision(recorded_turn), routed_decision(routed.result),
            ))
    return comparisons


def load_notes(path: Path = NOTES_PATH) -> list[dict]:
    notes = json.loads(path.read_text(encoding="utf-8"))
    seen: set[tuple[str, int]] = set()
    for index, note in enumerate(notes):
        if set(note) != NOTE_KEYS:
            raise ValueError(f"note {index} must have exactly the keys {sorted(NOTE_KEYS)}")
        if note["resolution"] not in RESOLUTIONS:
            raise ValueError(f"note {index} has unknown resolution {note['resolution']!r}")
        reason = note["reason"].strip()
        if len(reason) < 60 or reason.lower().rstrip(".") in {"v1 limitation", "known difference", "expected"}:
            raise ValueError(f"note {index} needs a concrete reason")
        location = (note["case"], note["turn"])
        if location in seen:
            raise ValueError(f"note {index} repeats {location}")
        seen.add(location)
    return notes


def report(comparisons: list[TurnComparison], notes: list[dict]) -> dict[str, list]:
    differing = {(c.case, c.turn): c for c in comparisons if c.differs}
    by_location = {(n["case"], n["turn"]): n for n in notes}
    unexplained = [c for location, c in differing.items() if location not in by_location]
    stale = [n for location, n in by_location.items() if location not in differing]
    outdated = [
        (n, differing[location]) for location, n in by_location.items()
        if location in differing and (
            n["expected"] != differing[location].expected
            or n["actual"] != differing[location].actual
            or n["message"] != differing[location].message
        )
    ]
    return {"unexplained": unexplained, "stale": stale, "outdated": outdated}


if __name__ == "__main__":
    import sys

    for comparison in compare_all(int(sys.argv[1]) if len(sys.argv) > 1 else 1):
        marker = "DIFF" if comparison.differs else "same"
        print(f"{marker} {comparison.case}[{comparison.turn}] {comparison.message!r}")
        if comparison.differs:
            print(f"     expected {json.dumps(comparison.expected)}")
            print(f"     actual   {json.dumps(comparison.actual)}")
    _ = REPO_ROOT
