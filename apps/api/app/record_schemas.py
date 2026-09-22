from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RecordInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IssueInput(RecordInput):
    revision: int | None = Field(default=None, ge=1)
    id: str = Field(default="", max_length=100)
    title: str = Field(min_length=1, max_length=300)
    priority: str = Field(min_length=1, max_length=30)
    assignee: str = Field(min_length=1, max_length=100)
    project: str = Field(min_length=1, max_length=200)
    projectId: str | None = Field(default=None, max_length=100)
    status: str = Field(min_length=1, max_length=40)
    cycle: str | None = Field(default=None, max_length=200)
    estimate: str | None = Field(default=None, max_length=30)
    label: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=10000)


class ProjectInput(RecordInput):
    revision: int | None = Field(default=None, ge=1)
    id: str = Field(default="", max_length=100)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=10000)
    progress: int = Field(ge=0, le=100)
    status: str = Field(min_length=1, max_length=40)
    lead: str = Field(min_length=1, max_length=100)
    team: str = Field(min_length=1, max_length=100)
    targetDate: date


class CycleInput(RecordInput):
    revision: int | None = Field(default=None, ge=1)
    id: str = Field(default="", max_length=100)
    name: str = Field(min_length=1, max_length=200)
    projectId: str | None = Field(default=None, max_length=100)
    daysLeft: int = Field(ge=0)
    progress: int = Field(ge=0, le=100)
    completed: int = Field(ge=0)
    inProgress: int = Field(ge=0)
    remaining: int = Field(ge=0)
    focus: list[str] = Field(max_length=50)
    status: str = Field(min_length=1, max_length=40)
    team: str = Field(min_length=1, max_length=100)
    startDate: date
    endDate: date

    @model_validator(mode="after")
    def check_dates(self):
        if self.endDate < self.startDate:
            raise ValueError("End date must not precede start date.")
        return self


class MemberInput(RecordInput):
    revision: int | None = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=100)
    initials: str = Field(min_length=1, max_length=8)
    role: str = Field(min_length=1, max_length=100)
    load: int = Field(ge=0, le=100)
    email: str | None = Field(default=None, max_length=254)
    projectIds: list[str] = Field(default_factory=list, max_length=100)


# --- 5b: keyed assistant writes carry only the change an execution key bound (plan, section 7.2) ---

KEYED_UPDATE_FIELDS = frozenset({"assignee", "priority", "status"})
KEYED_CREATE_FIELDS = frozenset({"title", "priority", "assignee", "project", "status"})


class KeyedIssueUpdate(BaseModel):
    """The fields an update key changes, and nothing else; the server applies them to the record."""

    model_config = ConfigDict(extra="forbid")

    changes: dict[str, str] = Field(min_length=1, max_length=len(KEYED_UPDATE_FIELDS))

    @model_validator(mode="after")
    def _known_fields(self) -> "KeyedIssueUpdate":
        unknown = set(self.changes) - KEYED_UPDATE_FIELDS
        if unknown:
            raise ValueError(f"these fields cannot be changed by an assistant write: {sorted(unknown)}")
        return self


class KeyedIssueCreate(BaseModel):
    """The fields a create key supplies; the server assigns the ID and the workspace fields."""

    model_config = ConfigDict(extra="forbid")

    fields: dict[str, str] = Field(min_length=1, max_length=len(KEYED_CREATE_FIELDS))

    @model_validator(mode="after")
    def _known_fields(self) -> "KeyedIssueCreate":
        unknown = set(self.fields) - KEYED_CREATE_FIELDS
        if unknown:
            raise ValueError(f"these fields cannot be set by an assistant write: {sorted(unknown)}")
        return self
