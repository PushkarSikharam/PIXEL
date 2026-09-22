from __future__ import annotations

from typing import Any

from app.product_config import PRODUCTS_BY_ID
from app.schemas import ProposedAction, ValidatedAction
from app.services.demo_data import (
    assignee_in_scope,
    issue_id_in_scope,
    load_demo_issues,
    team_member_in_scope,
)
from app.workspace_config import DEFAULT_WORKSPACE_SCOPE_ID, get_workspace_scope, WorkspaceScope


class ActionValidator:
    def validate(
        self,
        product_id: str,
        proposed_action: ProposedAction | None,
        workspace_scope_id: str = DEFAULT_WORKSPACE_SCOPE_ID,
        data: dict | None = None,
    ) -> ValidatedAction | None:
        if proposed_action is None:
            return None

        product = PRODUCTS_BY_ID.get(product_id)
        if product is None:
            return None

        if proposed_action.type not in product.allowed_actions:
            return None

        workspace_scope = get_workspace_scope(workspace_scope_id, data)
        if workspace_scope is None:
            return None

        payload = self._safe_payload(
            proposed_action.type, proposed_action.payload, workspace_scope, data
        )
        if payload is None:
            return None

        return ValidatedAction(type=proposed_action.type, payload=payload)

    def _safe_payload(
        self,
        action_type: str,
        payload: dict[str, Any],
        workspace_scope: WorkspaceScope,
        data: dict | None = None,
    ) -> dict[str, Any] | None:
        if action_type in {
            "OPEN_DASHBOARD",
            "OPEN_ISSUES",
            "OPEN_PROJECTS",
            "OPEN_CYCLES",
            "OPEN_TEAMS",
            "OPEN_INTEGRATIONS",
            "OPEN_SYSTEM_ARCHITECTURE",
            "HIGHLIGHT_CYCLE_PROGRESS",
            "OPEN_GITHUB_SETUP",
            "HIGHLIGHT_GITHUB_CARD",
            "HIGHLIGHT_SLACK_CARD",
        }:
            return {}

        if action_type == "HIGHLIGHT_CREATE_TICKET_BUTTON":
            return self._safe_ticket_prefill(payload, workspace_scope)

        if action_type == "HIGHLIGHT_ADD_MEMBER_BUTTON":
            name = payload.get("name")
            if name is None:
                return {}
            if isinstance(name, str) and 1 <= len(name) <= 80:
                return {"name": name}
            return None

        if action_type == "CREATE_DEMO_ISSUE":
            return self._safe_demo_issue_payload(payload, workspace_scope, data)

        if action_type == "UPDATE_DEMO_ISSUE":
            return self._safe_issue_update_payload(payload, workspace_scope, data)

        if action_type == "OPEN_DEMO_ISSUE":
            issue_id = payload.get("issue_id")
            if (
                isinstance(issue_id, str)
                and issue_id_in_scope(
                    issue_id,
                    set(workspace_scope.allowed_project_ids),
                    set(workspace_scope.allowed_issue_projects),
                    data,
                )
            ):
                return {"issue_id": issue_id}
            return None

        if action_type == "FILTER_ISSUES_BY_ASSIGNEE":
            assignee = payload.get("assignee")
            if (
                isinstance(assignee, str)
                and assignee_in_scope(
                    assignee,
                    set(workspace_scope.allowed_project_ids),
                    set(workspace_scope.allowed_issue_projects),
                    data,
                )
            ):
                return {"assignee": assignee}
            return None

        if action_type == "HIGHLIGHT_ASSIGNMENT_CONTROL":
            issue_id = payload.get("issue_id")
            if issue_id is None:
                return {}
            if (
                isinstance(issue_id, str)
                and issue_id_in_scope(
                    issue_id,
                    set(workspace_scope.allowed_project_ids),
                    set(workspace_scope.allowed_issue_projects),
                    data,
                )
            ):
                return {"issue_id": issue_id}
            return None

        return None

    def _safe_ticket_prefill(self, payload: dict[str, Any], workspace_scope: WorkspaceScope) -> dict[str, Any] | None:
        """What the ticket form may be prefilled with: only what the visitor said, only if valid here.

        A prefill is shown for review in the form and creates nothing.
        """
        safe: dict[str, Any] = {}
        assignee = payload.get("assignee")
        if assignee is not None:
            if assignee not in workspace_scope.allowed_team_members:
                return None
            safe["assignee"] = assignee
        priority = payload.get("priority")
        if priority is not None:
            if priority not in {"Low", "Medium", "High"}:
                return None
            safe["priority"] = priority
        title = payload.get("title")
        if title is not None:
            if not isinstance(title, str) or not 1 <= len(title) <= 120:
                return None
            safe["title"] = title
        return safe

    def _safe_demo_issue_payload(
        self,
        payload: dict[str, Any],
        workspace_scope: WorkspaceScope,
        data: dict | None = None,
    ) -> dict[str, Any] | None:
        issue_id = payload.get("id")
        title = payload.get("title")
        priority = payload.get("priority")
        assignee = payload.get("assignee")
        project = payload.get("project")
        project_id = payload.get("projectId")
        status = payload.get("status")

        if not (
            isinstance(issue_id, str)
            and (issue_id.startswith("PIX-") or issue_id.startswith("LIN-"))
            and isinstance(title, str)
            and 3 <= len(title) <= 120
            and priority in {"Low", "Medium", "High"}
            and isinstance(assignee, str)
            and 2 <= len(assignee) <= 80
            and isinstance(project, str)
            and 3 <= len(project) <= 80
            and status in {"Todo", "Backlog", "In progress"}
        ):
            return None

        if isinstance(project_id, str) and project_id not in workspace_scope.allowed_project_ids:
            return None
        if not isinstance(project_id, str) and project not in workspace_scope.allowed_issue_projects:
            return None
        allowed_project_ids = set(workspace_scope.allowed_project_ids)
        if assignee not in workspace_scope.allowed_team_members and not team_member_in_scope(
            assignee,
            allowed_project_ids,
            data,
        ):
            return None

        return {
            "id": issue_id,
            "title": title,
            "priority": priority,
            "assignee": assignee,
            "project": project,
            **({"projectId": project_id} if isinstance(project_id, str) else {}),
            "status": status,
        }

    def _safe_issue_update_payload(
        self,
        payload: dict[str, Any],
        workspace_scope: WorkspaceScope,
        data: dict | None = None,
    ) -> dict[str, Any] | None:
        issue_id = payload.get("issue_id")
        if not (
            isinstance(issue_id, str)
            and issue_id_in_scope(
                issue_id,
                set(workspace_scope.allowed_project_ids),
                set(workspace_scope.allowed_issue_projects),
                data,
            )
        ):
            return None

        safe_payload: dict[str, Any] = {"issue_id": issue_id}
        assignee = payload.get("assignee")
        priority = payload.get("priority")
        status = payload.get("status")
        allowed_project_ids = set(workspace_scope.allowed_project_ids)
        known_assignees = {issue.assignee for issue in load_demo_issues(data)}

        if assignee is not None:
            if not (
                isinstance(assignee, str)
                and (assignee in known_assignees or team_member_in_scope(assignee, allowed_project_ids, data))
                and (
                    assignee in workspace_scope.allowed_team_members
                    or team_member_in_scope(assignee, allowed_project_ids, data)
                )
            ):
                return None
            safe_payload["assignee"] = assignee

        if priority is not None:
            if priority not in {"Low", "Medium", "High"}:
                return None
            safe_payload["priority"] = priority

        if status is not None:
            if status not in {"Todo", "In progress", "Review", "Done"}:
                return None
            safe_payload["status"] = status

        return safe_payload if len(safe_payload) > 1 else None
