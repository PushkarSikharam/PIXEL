from __future__ import annotations

import re

from app.schemas import IntentTrace, ProposedAction
from app.services.demo_data import (
    extract_requested_assignee,
    find_issue_by_person,
    load_demo_issues,
    load_team_member_names,
    team_member_exists,
)
from app.services.language_normalizer import normalize_for_intent


class ActionPlanner:
    def plan(
        self,
        message: str,
        intent_trace: IntentTrace,
        selected_issue_id: str | None = None,
        allowed_issue_projects: set[str] | None = None,
        data: dict | None = None,
    ) -> ProposedAction | None:
        text = normalize_for_intent(message)

        if self._mentions_system_architecture(text):
            return ProposedAction(type="OPEN_SYSTEM_ARCHITECTURE")
        if "salesforce" in text:
            return ProposedAction(type="OPEN_SALESFORCE")
        if "gmail" in text or "email" in text:
            return ProposedAction(type="OPEN_GMAIL")
        if "delete" in text or "remove all" in text:
            return ProposedAction(type="DELETE_ISSUES")
        if self._asks_about_new_issue_workflow(text):
            return ProposedAction(type="HIGHLIGHT_CREATE_TICKET_BUTTON")
        if self._mentions_new_issue_request(text):
            assignee = extract_requested_assignee(message, data)
            if not team_member_exists(assignee, data):
                return ProposedAction(
                    type="HIGHLIGHT_ADD_MEMBER_BUTTON",
                    payload={"name": assignee},
                )
            return ProposedAction(
                type="CREATE_DEMO_ISSUE",
                payload=self._demo_issue_payload(message, allowed_issue_projects, data),
            )
        if self._mentions_github_setup(text):
            return ProposedAction(type="OPEN_GITHUB_SETUP")
        if "github" in text:
            return ProposedAction(type="HIGHLIGHT_GITHUB_CARD")
        if "slack" in text:
            return ProposedAction(type="HIGHLIGHT_SLACK_CARD")

        issue_update = self._issue_update_payload(message, text, selected_issue_id, data)
        if issue_update:
            return ProposedAction(type="UPDATE_DEMO_ISSUE", payload=issue_update)

        person_issue = find_issue_by_person(message, data)
        if person_issue and self._mentions_all_matching_tickets(text):
            return ProposedAction(
                type="FILTER_ISSUES_BY_ASSIGNEE",
                payload={"assignee": person_issue.assignee},
            )

        if self._mentions_assignment_workflow(text):
            return ProposedAction(
                type="HIGHLIGHT_ASSIGNMENT_CONTROL",
                payload={"issue_id": person_issue.id if person_issue else selected_issue_id or "LIN-142"},
            )

        if person_issue and self._mentions_ticket(text):
            return ProposedAction(
                type="OPEN_DEMO_ISSUE",
                payload={"issue_id": person_issue.id},
            )

        if selected_issue_id and self._mentions_current_issue(text):
            return ProposedAction(
                type="OPEN_DEMO_ISSUE",
                payload={"issue_id": selected_issue_id},
            )

        if intent_trace.relevant_feature == "Cycles":
            return ProposedAction(type="OPEN_CYCLES")
        if intent_trace.relevant_feature == "Issues":
            return ProposedAction(type="OPEN_ISSUES")
        if intent_trace.relevant_feature == "Projects":
            return ProposedAction(type="OPEN_PROJECTS")
        if intent_trace.relevant_feature == "Teams":
            return ProposedAction(type="OPEN_TEAMS")
        if intent_trace.relevant_feature == "Integrations":
            return ProposedAction(type="OPEN_INTEGRATIONS")

        return None

    def _mentions_ticket(self, text: str) -> bool:
        return any(term in text for term in ("ticket", "issue", "bug"))

    def _mentions_system_architecture(self, text: str) -> bool:
        return any(
            phrase in text
            for phrase in (
                "system architecture",
                "technical architecture",
                "product architecture",
                "architecture page",
                "open architecture",
                "show architecture",
                "view architecture",
            )
        )

    def _mentions_all_matching_tickets(self, text: str) -> bool:
        return self._mentions_ticket(text) and any(term in text for term in ("all", "any", "assigned to her", "assigned to him"))

    def _mentions_new_issue_request(self, text: str) -> bool:
        if not self._mentions_ticket(text):
            return False
        return bool(
            re.search(
                r"\b(create|new|fresh|make|add|raise|file)\b",
                text,
            )
        )

    def _asks_about_new_issue_workflow(self, text: str) -> bool:
        if not self._mentions_ticket(text):
            return False
        if not re.search(r"\b(create|new|fresh|make|add|raise|file)\b", text):
            return False
        return any(
            phrase in text
            for phrase in ("best way", "where", "how do i", "how to", "show me how", "show how")
        )

    def _mentions_assignment_workflow(self, text: str) -> bool:
        return (
            "how do i assign" in text
            or "how to assign" in text
            or "show assignment" in text
            or "issue assignment" in text
            or "highlight assignee" in text
            or "assign this" in text
        )

    def _issue_update_payload(
        self,
        message: str,
        text: str,
        selected_issue_id: str | None,
        data: dict | None = None,
    ) -> dict[str, str] | None:
        if "how do i assign" in text or "how to assign" in text or "show assignment" in text or "issue assignment" in text:
            return None

        issue_id = self._target_issue_id(message, selected_issue_id, data)
        if not issue_id:
            return None

        payload: dict[str, str] = {"issue_id": issue_id}
        assignee = self._assignment_target(message, data)
        if assignee:
            payload["assignee"] = assignee

        priority = self._demo_issue_priority(text) if self._mentions_priority_update(text) else None
        if priority:
            payload["priority"] = priority

        status = self._status_target(text)
        if status:
            payload["status"] = status

        return payload if len(payload) > 1 else None

    def _target_issue_id(self, message: str, selected_issue_id: str | None,
                         data: dict | None = None) -> str | None:
        explicit = re.search(r"\b(?:LIN|PIX)-\d+\b", message, re.IGNORECASE)
        if explicit:
            return explicit.group(0).upper()

        if selected_issue_id and re.search(r"\b(this|that|it|current|same)\b", message, re.IGNORECASE):
            return selected_issue_id

        # "Assign Maya's ticket to Noah": the ticket is Maya's. The person after "to" is the new
        # owner and is never read as the ticket to change.
        owner = re.search(r"\b([a-zA-Z]+)'s\s+(?:ticket|issue|bug)\b", message, re.IGNORECASE)
        if owner:
            owned = find_issue_by_person(owner.group(1), data)
            if owned:
                return owned.id
        without_new_owner = re.sub(
            r"\b(?:assign(?:ed)?\s+(?:it\s+|this\s+)?to|owner is|assignee is|reassign\s+\S+\s+to)\s+[a-zA-Z]+(?:\s+[a-zA-Z]+)?",
            " ", message, flags=re.IGNORECASE,
        )
        person_issue = find_issue_by_person(without_new_owner, data)
        if person_issue:
            return person_issue.id

        if selected_issue_id and re.search(r"\b(her|his)\b", message, re.IGNORECASE):
            return selected_issue_id

        return selected_issue_id

    def _assignment_target(self, message: str, data: dict | None = None) -> str | None:
        if not re.search(r"\b(assign|reassign|owner|assignee)\b", message, re.IGNORECASE):
            return None
        match = re.search(
            r"\b(?:to|owner is|assignee is|assigned to)\s+([a-zA-Z]+(?:\s+[a-zA-Z]+)?)",
            message,
            re.IGNORECASE,
        )
        if not match:
            return None

        candidate = re.sub(
            r"\b(ticket|issue|bug|priority|status|review|done|todo)\b",
            "",
            match.group(1),
            flags=re.IGNORECASE,
        ).strip()
        if not candidate:
            return None

        normalized_candidate = candidate.lower()
        for member in load_team_member_names(data):
            member_parts = {part.lower() for part in member.split()}
            member_parts.add(member.lower())
            if normalized_candidate in member_parts:
                return member
        return candidate.title()

    def _mentions_priority_update(self, text: str) -> bool:
        return any(term in text for term in ("priority", "urgent", "critical", "high", "medium", "low"))

    def _status_target(self, text: str) -> str | None:
        if any(term in text for term in ("done", "complete", "completed", "closed", "resolved")):
            return "Done"
        if "review" in text or "qa" in text:
            return "Review"
        if any(term in text for term in ("in progress", "doing", "started", "working")):
            return "In progress"
        if "todo" in text or "to do" in text or "backlog" in text:
            return "Todo"
        return None

    def _mentions_current_issue(self, text: str) -> bool:
        return any(phrase in text for phrase in ("this ticket", "this issue", "this bug"))

    def _mentions_github_setup(self, text: str) -> bool:
        return "github" in text and any(
            term in text
            for term in (
                "connect",
                "setup",
                "set up",
                "configure",
                "add",
                "install",
                "enable",
            )
        )

    def _demo_issue_payload(
        self,
        message: str,
        allowed_issue_projects: set[str] | None = None,
        data: dict | None = None,
    ) -> dict[str, str]:
        text = normalize_for_intent(message)
        assignee = extract_requested_assignee(message, data)
        return {
            "id": self._next_demo_issue_id(data),
            "title": self._demo_issue_title(message, data),
            "priority": self._demo_issue_priority(text),
            "assignee": assignee,
            "project": self._demo_issue_project(text, allowed_issue_projects),
            "status": "Todo",
        }

    def _next_demo_issue_id(self, data: dict | None = None) -> str:
        issue_numbers = []
        for issue in load_demo_issues(data):
            match = re.search(r"LIN-(\d+)|PIX-(\d+)", issue.id)
            if match:
                num = match.group(1) or match.group(2)
                if num:
                    issue_numbers.append(int(num))
        base_number = max(issue_numbers, default=142)
        # The materialized record snapshot is the source of truth. A process-global counter makes
        # one visitor's IDs depend on unrelated visitors and breaks resume after a refresh.
        next_number = base_number + 1
        return f"PIX-{next_number}"

    def _demo_issue_title(self, message: str, data: dict | None = None) -> str:
        text = normalize_for_intent(message)
        match = re.search(r"\b(?:about|title|regarding|named)\s+(.+)", message, re.IGNORECASE)
        if match:
            return match.group(1).strip().capitalize()

        if "login" in text or "sign in" in text:
            return "Investigate login issue"
        if "github" in text:
            return "Review GitHub sync issue"
        if "webhook" in text:
            return "Investigate webhook issue"
        if "bug" in text:
            return "Investigate reported bug"

        person = extract_requested_assignee(message, data)
        if person and person != "Maya Chen":
            return f"Investigate request for {person}"

        return "Investigate customer onboarding issue"

    def _demo_issue_priority(self, text: str) -> str:
        if any(term in text for term in ("urgent", "critical", "high")):
            return "High"
        if "low" in text:
            return "Low"
        return "Medium"

    def _demo_issue_project(self, text: str, allowed_issue_projects: set[str] | None = None) -> str:
        allowed_projects = allowed_issue_projects or {"Integrations", "Issue Triage", "Planning"}
        if (
            ("github" in text or "integration" in text or "webhook" in text)
            and "Integrations" in allowed_projects
        ):
            return "Integrations"
        if ("sprint" in text or "cycle" in text) and "Planning" in allowed_projects:
            return "Planning"
        if "Issue Triage" in allowed_projects:
            return "Issue Triage"
        return sorted(allowed_projects)[0]
