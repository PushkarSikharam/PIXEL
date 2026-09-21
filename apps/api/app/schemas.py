from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from app.workspace_config import DEFAULT_WORKSPACE_SCOPE_ID


AllowedActionType = Literal[
    "OPEN_DASHBOARD",
    "OPEN_ISSUES",
    "OPEN_PROJECTS",
    "OPEN_CYCLES",
    "OPEN_TEAMS",
    "OPEN_INTEGRATIONS",
    "OPEN_SYSTEM_ARCHITECTURE",
    "OPEN_DEMO_ISSUE",
    "CREATE_DEMO_ISSUE",
    "UPDATE_DEMO_ISSUE",
    "FILTER_ISSUES_BY_ASSIGNEE",
    "HIGHLIGHT_ASSIGNMENT_CONTROL",
    "HIGHLIGHT_CREATE_TICKET_BUTTON",
    "HIGHLIGHT_ADD_MEMBER_BUTTON",
    "HIGHLIGHT_CYCLE_PROGRESS",
    "OPEN_GITHUB_SETUP",
    "HIGHLIGHT_GITHUB_CARD",
    "HIGHLIGHT_SLACK_CARD",
]


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    turn_id: int = Field(ge=1)
    product_id: str = Field(min_length=1)
    message: str = Field(min_length=1, max_length=2000)
    input_mode: Literal["text", "voice"] = "text"
    current_page: str | None = None
    selected_issue_id: str | None = None
    workspace_scope_id: str = DEFAULT_WORKSPACE_SCOPE_ID

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("message cannot be blank")
        return normalized


class CancelTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ValidatedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: AllowedActionType
    payload: dict[str, Any] = Field(default_factory=dict)


class IntentTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str | None = None
    team_size: int | None = None
    current_tool: str | None = None
    goal: str | None = None
    pain_point: str | None = None
    current_intent: str | None = None
    relevant_feature: str | None = None
    reason: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    status: Literal["active", "interrupted", "denied"] = "active"


class Signal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    value: str
    confidence: float = Field(ge=0.0, le=1.0)


class RetrievedContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    source: str
    snippet: str


class SessionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interests: list[str] = Field(default_factory=list)
    pain_points: list[str] = Field(default_factory=list)
    last_person: str | None = None
    last_feature: str | None = None
    clarification_pending: str | None = None


class TurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    turn_id: int
    status: Literal["completed", "cancelled", "stale", "denied"]
    speech: str
    proposed_action: ProposedAction | None
    validated_action: ValidatedAction | None
    intent_trace: IntentTrace
    signals: list[Signal] = Field(default_factory=list)
    retrieved_context: list[RetrievedContext] = Field(default_factory=list)
    session_summary: SessionSummary = Field(default_factory=SessionSummary)
    # Server-side only, never serialized: whether this turn reached the conversation engine. A turn
    # refused or discarded before it could change the conversation is not context the 5a shadow
    # lost (5b plan, section 10.2).
    _engine_entered: bool = PrivateAttr(default=True)


class CancelTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    turn_id: int
    status: Literal["cancelled", "not_active"]
