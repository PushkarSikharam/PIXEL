from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkspaceScope:
    id: str
    name: str
    description: str
    allowed_project_ids: frozenset[str]
    allowed_issue_projects: frozenset[str]
    allowed_team_members: frozenset[str]


WORKSPACE_SCOPES: tuple[WorkspaceScope, ...] = (
    WorkspaceScope(
        id="workspace-product-eng",
        name="Product Engineering Workspace",
        description="Scoped to Integrations and Issue Triage project work.",
        allowed_project_ids=frozenset({"PRJ-101", "PRJ-102"}),
        allowed_issue_projects=frozenset({"Integrations", "Issue Triage"}),
        allowed_team_members=frozenset({"Maya Chen", "Noah Patel"}),
    ),
    WorkspaceScope(
        id="workspace-platform",
        name="Platform Workspace",
        description="Scoped to planning insights, migration work, and platform reliability.",
        allowed_project_ids=frozenset({"PRJ-103", "PRJ-104"}),
        allowed_issue_projects=frozenset({"Planning", "Migration"}),
        allowed_team_members=frozenset({"Avery Brooks", "Iris Morgan"}),
    ),
)

WORKSPACE_SCOPES_BY_ID = {scope.id: scope for scope in WORKSPACE_SCOPES}
DEFAULT_WORKSPACE_SCOPE_ID = "workspace-product-eng"


def get_workspace_scope(scope_id: str, data: dict | None = None) -> WorkspaceScope | None:
    """Resolve a workspace from the caller's own records, so newly created projects are included.

    There is no default dataset: a private visitor's turn must never resolve its workspace from
    the shared member records.
    """
    if data is None:
        raise ValueError("workspace lookups need the caller's own records")
    row = next((scope for scope in data.get("workspaceScopes", []) if scope["id"] == scope_id), None)
    if row is None:
        return None
    project_ids = frozenset(row["allowedProjectIds"])
    return WorkspaceScope(
        row["id"], row["name"], row["description"], project_ids,
        frozenset(row["allowedIssueProjects"]),
        frozenset(member["name"] for member in data.get("team", [])
                  if project_ids.intersection(member.get("projectIds", []))),
    )
