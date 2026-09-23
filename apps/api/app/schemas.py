from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator

from app.workspace_config import DEFAULT_WORKSPACE_SCOPE_ID




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
    """An action a caller is actually sent.

    The type names one action of the product that answered, and is produced only by that
    product's translator from an action its definition declares and the validator accepted. That
    is where the set of possible types is closed; a product added to Pixel brings its own, so no
    list here could know them. What is checked here is the shape, so a malformed name cannot be
    carried to a client whatever produced it.
    """

    model_config = ConfigDict(extra="forbid")

    type: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
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


class ExecutionEnvelope(BaseModel):
    """The one-time key for a dispatched mutation (5b plan, section 5.1). Never inside a payload.

    `expires_at` is advisory; the server decides. `turn_id` is for correlation, never authorization.
    """

    model_config = ConfigDict(extra="forbid")

    key: str
    session_id: str
    turn_id: int
    expires_at: str


class ExecutionReceipt(BaseModel):
    """What a keyed write reports, composed by the platform from the committed outcome only."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["executed", "failed", "refused"]
    code: str
    speech: str
    record: dict[str, Any] | None = None


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
    # 5b: a key only for a mutation dispatched on this turn; null for everything else. The live
    # engine dispatches nothing before 5c, so it is null on every live response.
    execution: ExecutionEnvelope | None = None
    # Server-side only, never serialized: whether this turn reached the conversation engine. A turn
    # refused or discarded before it could change the conversation is not context the 5a shadow
    # lost (5b plan, section 10.2).
    _engine_entered: bool = PrivateAttr(default=True)
    # What the optional model gateway did on this turn, for telemetry only (never serialized).
    _model_outcome: str | None = PrivateAttr(default=None)


class CancelTurnResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    turn_id: int
    status: Literal["cancelled", "not_active"]
