"""Temporary translation of validated generic actions into today's action names (3.2 plan, §9).

The current web app understands one fixed list of action types with product-specific payload
keys. Until 3.6 replaces it with a generic adapter, this translator sits in the product package
and converts a *validated* action into that legacy shape. It is the only place that knows those
names, and it fails closed: an action the table does not cover raises `TranslationMissing`
rather than producing a guess, or a weaker action that merely looks similar. A create is never
translated into a highlight.

The generic action carries person IDs (`maya-chen`); the legacy payload carries display names
(`Maya Chen`), so the caller's scope-bound lookup resolves them. A person the caller cannot see
cannot be translated, which keeps the scope boundary intact on this path too.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.engine.lookup import RecordLookup
from app.engine.validator import ValidatedAction

# Definition action -> today's action type. An action with no honest equivalent is absent, and
# translating it raises. `create_member` is deliberately missing: today's app can only highlight
# where members are added, and reporting that as a created member would be untrue.
LEGACY_TYPES = {
    "open_dashboard": "OPEN_DASHBOARD",
    "open_issues": "OPEN_ISSUES",
    "open_projects": "OPEN_PROJECTS",
    "open_cycles": "OPEN_CYCLES",
    "open_teams": "OPEN_TEAMS",
    "open_integrations": "OPEN_INTEGRATIONS",
    "open_architecture": "OPEN_SYSTEM_ARCHITECTURE",
    "open_issue": "OPEN_DEMO_ISSUE",
    "issues_by_assignee": "FILTER_ISSUES_BY_ASSIGNEE",
    "create_issue": "CREATE_DEMO_ISSUE",
    "update_issue": "UPDATE_DEMO_ISSUE",
    "highlight_assignment": "HIGHLIGHT_ASSIGNMENT_CONTROL",
    "highlight_create_issue": "HIGHLIGHT_CREATE_TICKET_BUTTON",
    "highlight_add_member": "HIGHLIGHT_ADD_MEMBER_BUTTON",
    "highlight_cycle_progress": "HIGHLIGHT_CYCLE_PROGRESS",
    "open_github_setup": "OPEN_GITHUB_SETUP",
    "highlight_github": "HIGHLIGHT_GITHUB_CARD",
    "highlight_slack": "HIGHLIGHT_SLACK_CARD",
}
# Generic field -> legacy payload key, for the fields today's payloads carry.
FIELD_KEYS = {
    "title": "title", "priority": "priority", "assignee": "assignee",
    "project": "project", "status": "status", "name": "name",
}
# Legacy payload keys whose value is a person reference, and so needs a display name. A member's
# own `name` field is plain text, not a reference: the person may not exist yet.
PERSON_KEYS = frozenset({"assignee", "lead"})


class TranslationMissing(LookupError):
    """This validated action has no legacy equivalent; nothing is sent to the client."""


@dataclass(frozen=True)
class LegacyAction:
    type: str
    payload: dict[str, Any]


class LinearLegacyTranslator:
    def __init__(self, lookup: RecordLookup) -> None:
        self._lookup = lookup

    def can_translate(self, action_key: str) -> bool:
        """Whether today's app can express this action at all; offers are limited to these."""
        return action_key in LEGACY_TYPES

    def translate(self, validated: ValidatedAction) -> LegacyAction:
        action = validated.action
        legacy_type = LEGACY_TYPES.get(action.action_key)
        if legacy_type is None:
            raise TranslationMissing(action.action_key)
        builder: Callable[[Any], dict[str, Any]] = getattr(
            self, f"_payload_{action.action_key}", self._no_payload
        )
        return LegacyAction(legacy_type, builder(action))

    # --- payloads ---

    def _no_payload(self, action) -> dict[str, Any]:
        return {}

    def _payload_open_issue(self, action) -> dict[str, Any]:
        return {"issue_id": self._target(action)}

    def _payload_highlight_assignment(self, action) -> dict[str, Any]:
        # v2 shows the assignment control without choosing a ticket for the visitor; the web app
        # accepts the action with no ticket.
        return {"issue_id": self._target(action)} if action.target is not None else {}

    def _payload_issues_by_assignee(self, action) -> dict[str, Any]:
        if action.filter is None:
            raise TranslationMissing("issues_by_assignee without a filter")
        return {"assignee": self._person_name(str(action.filter.value))}

    def _payload_create_issue(self, action) -> dict[str, Any]:
        return self._fields(action, action.fields)

    def _payload_update_issue(self, action) -> dict[str, Any]:
        return {"issue_id": self._target(action), **self._fields(action, action.fields)}

    def _payload_highlight_create_issue(self, action) -> dict[str, Any]:
        return self._fields(action, action.prefill)

    def _payload_highlight_add_member(self, action) -> dict[str, Any]:
        return self._fields(action, action.prefill)

    # --- helpers ---

    def _target(self, action) -> str:
        if action.target is None:
            raise TranslationMissing(f"{action.action_key} without a target record")
        return action.target.id

    def _fields(self, action, values) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for name, value in (values or {}).items():
            key = FIELD_KEYS.get(name)
            if key is None:
                raise TranslationMissing(f"{action.action_key} field {name}")
            payload[key] = self._person_name(str(value)) if key in PERSON_KEYS else value
        return payload

    def _person_name(self, person_id: str) -> str:
        """A person the caller cannot see has no name here, and the action cannot be sent."""
        record = self._lookup.get("member", person_id)
        if record is None:
            raise TranslationMissing(f"no visible person {person_id}")
        return record.title


def translator_for(lookup: RecordLookup) -> LinearLegacyTranslator:
    return LinearLegacyTranslator(lookup)
