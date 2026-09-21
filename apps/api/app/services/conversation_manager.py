from __future__ import annotations

from dataclasses import dataclass

from app.schemas import IntentTrace, ProposedAction, Signal
from app.services.demo_data import find_issue_by_person
from app.services.language_normalizer import normalize_for_intent


@dataclass(frozen=True)
class ConversationDecision:
    speech: str
    intent_trace: IntentTrace
    signals: list[Signal]
    clarification_pending: str | None = None


class ConversationManager:
    def clarification_for(self, message: str) -> ConversationDecision | None:
        text = normalize_for_intent(message)

        if text in {"open all", "open all the", "all", "all the", "show all the"}:
            return ConversationDecision(
                speech=(
                    "Do you mean all issues, all projects, or all tickets for a specific person?"
                ),
                intent_trace=IntentTrace(
                    goal="Clarify request",
                    current_intent="Clarification needed",
                    reason="The visitor asked for all items but did not specify what to show.",
                    confidence=0.58,
                    status="active",
                ),
                signals=[],
                clarification_pending="all_items",
            )

        return None

    def follow_up_action(
        self,
        message: str,
        last_feature: str | None,
        data: dict | None = None,
    ) -> tuple[ProposedAction | None, list[Signal]]:
        text = normalize_for_intent(message)
        issue = find_issue_by_person(message, data)

        if issue:
            signals = [
                Signal(type="person_interest", value=issue.assignee, confidence=0.84),
            ]
            if last_feature == "issues" and text.startswith("what about"):
                return (
                    ProposedAction(
                        type="FILTER_ISSUES_BY_ASSIGNEE",
                        payload={"assignee": issue.assignee},
                    ),
                    signals,
                )
            return None, signals

        return None, []
