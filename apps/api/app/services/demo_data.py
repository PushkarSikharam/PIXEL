from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.services.language_normalizer import normalize_for_intent


@dataclass(frozen=True)
class DemoIssue:
    id: str
    title: str
    priority: str
    assignee: str
    project: str
    status: str
    projectId: str | None = None


ROOT_DIR = Path(__file__).resolve().parents[4]
ISSUES_PATH = ROOT_DIR / "packages" / "shared" / "demo-data" / "issues.json"


def load_demo_issues(data: dict | None = None) -> tuple[DemoIssue, ...]:
    if data is not None:
        return tuple(_issue_from_payload(issue) for issue in data.get("issues", []))
    try:
        from app.services.product_data_store import ProductDataStore

        raw_issues = ProductDataStore().load()["issues"]
    except Exception:
        raw_issues = json.loads(ISSUES_PATH.read_text(encoding="utf-8"))
    return tuple(_issue_from_payload(issue) for issue in raw_issues)


def issue_exists(issue_id: str, data: dict | None = None) -> bool:
    return any(issue.id == issue_id for issue in load_demo_issues(data))


def find_issue_by_id(issue_id: str, data: dict | None = None) -> DemoIssue | None:
    normalized_issue_id = issue_id.upper()
    for issue in load_demo_issues(data):
        if issue.id.upper() == normalized_issue_id:
            return issue
    return None


def issue_in_scope(issue: DemoIssue, allowed_project_ids: set[str], allowed_issue_projects: set[str]) -> bool:
    # A project ID is authoritative. Names are only a fallback for issues without one,
    # because a same-named project in another workspace must never widen access.
    if issue.projectId:
        return issue.projectId in allowed_project_ids
    return issue.project in allowed_issue_projects


def issue_id_in_scope(issue_id: str, allowed_project_ids: set[str], allowed_issue_projects: set[str],
                      data: dict | None = None) -> bool:
    issue = find_issue_by_id(issue_id, data)
    return bool(issue and issue_in_scope(issue, allowed_project_ids, allowed_issue_projects))


def assignee_in_scope(assignee: str, allowed_project_ids: set[str], allowed_issue_projects: set[str],
                      data: dict | None = None) -> bool:
    return any(
        issue.assignee == assignee and issue_in_scope(issue, allowed_project_ids, allowed_issue_projects)
        for issue in load_demo_issues(data)
    )


def team_member_in_scope(assignee: str, allowed_project_ids: set[str], data: dict | None = None) -> bool:
    if data is not None:
        members = data.get("team", [])
    else:
        try:
            from app.services.product_data_store import ProductDataStore

            members = ProductDataStore().load()["team"]
        except Exception:
            return False

    normalized_assignee = _normalize(assignee)
    for member in members:
        if _normalize(str(member.get("name", ""))) != normalized_assignee:
            continue
        project_ids = set(member.get("projectIds") or [])
        if project_ids.intersection(allowed_project_ids):
            return True
    return False


def team_member_exists(name: str, data: dict | None = None) -> bool:
    if data is not None:
        members = data.get("team", [])
    else:
        try:
            from app.services.product_data_store import ProductDataStore

            members = ProductDataStore().load()["team"]
        except Exception:
            return name in {"Maya Chen", "Noah Patel", "Avery Brooks", "Iris Morgan"}

    normalized_name = _normalize(name)
    return any(_normalize(str(member.get("name", ""))) == normalized_name for member in members)


def load_team_member_names(data: dict | None = None) -> tuple[str, ...]:
    if data is not None:
        members = data.get("team", [])
    else:
        try:
            from app.services.product_data_store import ProductDataStore

            members = ProductDataStore().load()["team"]
        except Exception:
            return ("Maya Chen", "Noah Patel", "Avery Brooks", "Iris Morgan")
    names = [str(member.get("name", "")) for member in members]
    return tuple(name for name in names if name)


def find_issues_by_person(message: str, data: dict | None = None) -> tuple[DemoIssue, ...]:
    normalized_message = normalize_for_intent(message)
    matched_issues: list[DemoIssue] = []
    for issue in load_demo_issues(data):
        assignee_parts = _name_parts(issue.assignee)
        if any(part in normalized_message for part in assignee_parts):
            matched_issues.append(issue)
    return tuple(matched_issues)


def find_issue_by_person(message: str, data: dict | None = None) -> DemoIssue | None:
    issues = find_issues_by_person(message, data)
    return issues[0] if issues else None


def find_issues_by_person_in_scope(
    message: str,
    allowed_project_ids: set[str],
    allowed_issue_projects: set[str],
    data: dict | None = None,
) -> tuple[DemoIssue, ...]:
    return tuple(
        issue
        for issue in find_issues_by_person(message, data)
        if issue_in_scope(issue, allowed_project_ids, allowed_issue_projects)
    )


def find_issue_by_person_in_scope(
    message: str,
    allowed_project_ids: set[str],
    allowed_issue_projects: set[str],
    data: dict | None = None,
) -> DemoIssue | None:
    issues = find_issues_by_person_in_scope(
        message, allowed_project_ids, allowed_issue_projects, data
    )
    return issues[0] if issues else None


def extract_unknown_person(message: str) -> str | None:
    patterns = (
        r"\b(?:for|assigned to|assign to|created for|ticket for|issue for)\s+([a-zA-Z]+(?:\s+[a-zA-Z]+)?)",
        r"\b([a-zA-Z]+)'s\s+(?:ticket|issue|bug)",
    )
    stop_words = {"about", "regarding", "named", "with", "on", "for", "to", "in", "at", "login", "bug", "issue", "ticket", "fresh", "new"}
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            words = candidate.split()
            filtered_words = []
            for w in words:
                if w.lower() in stop_words:
                    break
                filtered_words.append(w)
            if filtered_words:
                result = " ".join(filtered_words).title()
                if result.lower() not in {"jira", "linear", "salesforce", "github", "slack", "me", "us", "a", "the", "user", "users"}:
                    return result
    return None


def extract_requested_assignee(message: str, data: dict | None = None) -> str:
    issue = find_issue_by_person(message, data)
    if issue:
        return issue.assignee

    unknown = extract_unknown_person(message)
    if unknown:
        return unknown

    return "Maya Chen"


def _name_parts(name: str) -> set[str]:
    normalized_name = _normalize(name)
    parts = set(normalized_name.split())
    parts.add(normalized_name)
    return parts


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", value.lower()).strip()


def _issue_from_payload(issue: dict) -> DemoIssue:
    return DemoIssue(
        id=issue["id"],
        title=issue["title"],
        priority=issue["priority"],
        assignee=issue["assignee"],
        project=issue["project"],
        status=issue["status"],
        projectId=issue.get("projectId"),
    )
