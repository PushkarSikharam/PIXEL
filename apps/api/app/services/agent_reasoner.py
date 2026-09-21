from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
from dataclasses import dataclass
from typing import Any, Callable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.product_config import PRODUCTS_BY_ID
from app.schemas import IntentTrace, ProposedAction
from app.services.demo_data import issue_in_scope, load_demo_issues
from app.services import http_client
from app.services.env import env_bool, env_int, env_value
from app.services.retriever import RetrievedDocument
from app.services.provider_policy import ReasoningTokenLimits, reasoning_token_limits
from app.services.usage_ledger import (
    AccountingUnavailable,
    AttemptRequest,
    BudgetExceeded,
    UsageLedger,
    logger as usage_logger,
)
from app.tenancy import ProductContext
from app.workspace_config import WorkspaceScope


GeminiTransport = Callable[[str, dict[str, Any], int], dict[str, Any]]


class ReasonedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentReasoningResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speech: str = Field(min_length=1, max_length=700)
    proposed_action: ReasonedAction | None = None
    clarification_question: str | None = Field(default=None, max_length=220)
    intent_trace: IntentTrace


@dataclass(frozen=True)
class PromptSection:
    """A block of prompt text. ``trim_order`` is None for required sections; optional
    sections are dropped in ascending ``trim_order`` when the request is too large."""

    text: str
    trim_order: int | None = None


@dataclass(frozen=True)
class AgentReasoningContext:
    owner: ProductContext
    definition_id: str
    message: str
    current_page: str | None
    selected_issue_id: str | None
    workspace_scope: WorkspaceScope
    retrieved_docs: list[RetrievedDocument]
    user_id: str | None = None
    session_id: str | None = None
    request_id: str | None = None
    visible_data: dict[str, list[dict[str, Any]]] | None = None


class AgentReasoner:
    def __init__(
        self,
        transport: GeminiTransport | None = None,
        usage: UsageLedger | None = None,
    ) -> None:
        self.transport = transport or self._default_transport
        self.usage = usage or UsageLedger()

    def enabled(self) -> bool:
        provider = (env_value("LLM_PROVIDER") or "gemini").lower()
        return (
            provider == "gemini"
            and env_bool("LLM_ENABLED", default=True)
            and bool(env_value("GEMINI_API_KEY"))
        )

    def reason(self, context: AgentReasoningContext) -> AgentReasoningResult | None:
        api_key = env_value("GEMINI_API_KEY")
        if not self.enabled() or not api_key:
            return None

        timeout_ms = env_int("LLM_TIMEOUT_MS", 6000)
        tenant = context.owner
        limits = reasoning_token_limits(tenant)
        payload = self._payload(context, limits)
        if payload is None:
            usage_logger.info(json.dumps({
                "event": "reasoning_skipped", "reason": "input_too_large", "tenant_id": tenant.tenant_id,
            }))
            return None

        # One reservation per provider attempt; nothing is sent unless it succeeds.
        try:
            attempt_id = self.usage.reserve(AttemptRequest(
                tenant=tenant,
                user_id=context.user_id or "system",
                request_id=context.request_id or str(uuid4()),
                capability="reasoning",
                provider="gemini",
                model=self._model_name(),
                session_id=context.session_id,
                reserved_units=estimate_tokens(json.dumps(payload)) + limits.max_output_tokens,
            ))
        except (BudgetExceeded, AccountingUnavailable):
            return None

        started = time.monotonic()
        try:
            response = self.transport(api_key, payload, timeout_ms)
        except (OSError, TimeoutError, urllib.error.URLError, socket.timeout, ValueError) as error:
            status = "timeout" if _is_timeout(error) else "failed"
            self._settle(attempt_id, tenant, status, started, type(error).__name__)
            return None
        except BaseException as error:
            self._settle(attempt_id, tenant, "failed", started, type(error).__name__)
            raise

        input_tokens, output_tokens = _reported_tokens(response)
        if input_tokens is not None and input_tokens > limits.max_input_tokens:
            # Our estimate is conservative, so this should not happen; make it visible if it does.
            usage_logger.warning(json.dumps({
                "event": "reasoning_input_limit_exceeded", "attempt_id": attempt_id,
                "reported_input_tokens": input_tokens, "limit": limits.max_input_tokens,
            }))
        self._settle(attempt_id, tenant, "succeeded", started,
                     input_tokens=input_tokens, output_tokens=output_tokens)
        return self._parse_response(response)

    def _settle(
        self,
        attempt_id: str,
        tenant: ProductContext,
        status: str,
        started: float,
        reason: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        # The request was already dispatched, so a settle failure must not hide the result;
        # the reservation simply stays consumed.
        reported = [value for value in (input_tokens, output_tokens) if value is not None]
        try:
            self.usage.settle(
                attempt_id,
                tenant,
                status,
                duration_ms=int((time.monotonic() - started) * 1000),
                actual_units=sum(reported) if reported else None,
                actual_input_units=input_tokens,
                actual_output_units=output_tokens,
                reason=reason,
            )
        except Exception as error:
            usage_logger.warning(json.dumps({
                "event": "settle_failed", "attempt_id": attempt_id, "error": type(error).__name__,
            }))

    def _payload(self, context: AgentReasoningContext, limits: ReasoningTokenLimits) -> dict[str, Any] | None:
        """Build the request within the input-token limit, or return None if it cannot fit.

        Required sections (instructions, scope rules, the current request) are always kept.
        Optional context is dropped in trim order until the estimated request fits.
        """
        sections = self._prompt_sections(context)
        trimmable = sorted(
            (index for index, section in enumerate(sections) if section.trim_order is not None),
            key=lambda index: sections[index].trim_order,
        )
        dropped: set[int] = set()
        while True:
            prompt = "\n".join(section.text for index, section in enumerate(sections) if index not in dropped)
            payload = self._request_body(prompt, limits.max_output_tokens)
            if estimate_tokens(json.dumps(payload)) <= limits.max_input_tokens:
                return payload
            if not trimmable:
                return None
            dropped.add(trimmable.pop(0))

    def _request_body(self, prompt: str, max_output_tokens: int) -> dict[str, Any]:
        response_schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "speech": {"type": "string"},
                "proposed_action": {
                    "nullable": True,
                    "type": "object",
                    "properties": {
                        "type": {"type": "string"},
                        "payload": {"type": "object"},
                    },
                    "required": ["type", "payload"],
                },
                "clarification_question": {"nullable": True, "type": "string"},
                "intent_trace": {
                    "type": "object",
                    "properties": {
                        "role": {"nullable": True, "type": "string"},
                        "team_size": {"nullable": True, "type": "integer"},
                        "current_tool": {"nullable": True, "type": "string"},
                        "goal": {"nullable": True, "type": "string"},
                        "pain_point": {"nullable": True, "type": "string"},
                        "current_intent": {"nullable": True, "type": "string"},
                        "relevant_feature": {"nullable": True, "type": "string"},
                        "reason": {"nullable": True, "type": "string"},
                        "confidence": {"type": "number"},
                        "status": {"type": "string"},
                    },
                    "required": ["confidence", "status"],
                },
            },
            "required": ["speech", "proposed_action", "clarification_question", "intent_trace"],
        }

        model = self._model_name()
        generation_config: dict[str, Any] = {
            "temperature": 0.1,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
            "responseSchema": response_schema,
        }
        if model.startswith("gemini-3"):
            generation_config["thinkingConfig"] = {
                "thinkingLevel": env_value("LLM_THINKING_LEVEL") or "minimal",
            }
        elif model.startswith("gemini-2.5"):
            generation_config["thinkingConfig"] = {
                "thinkingBudget": env_int("LLM_THINKING_BUDGET", 0),
            }

        return {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": generation_config,
        }

    def _prompt_sections(self, context: AgentReasoningContext) -> list[PromptSection]:
        product = PRODUCTS_BY_ID[context.definition_id]
        visible = self._visible_workspace_data(context.workspace_scope, context.visible_data)
        docs = "\n".join(
            f"- {doc.title}: {doc.snippet}"
            for doc in context.retrieved_docs[:2]
        ) or "- No retrieved product docs matched. Use the visible workspace data and allowed actions only."

        return [
            PromptSection(
                "You are Edith, the conversational demo agent for Pixel.\n"
                "Understand the visitor's intent and return only the requested JSON schema.\n"
                "Never claim you completed an action. Say what you will open, show, update, or ask next.\n"
                "You may propose exactly one action, or null if a clarification is better.\n"
                "Do not expose private reasoning. intent_trace must be short and UI-safe.\n"
                "Stay inside the active workspace. If the visitor asks for other workspaces or external apps, "
                "propose no action and explain the boundary.\n"
            ),
            PromptSection(
                f"Product: {product.name}\n"
                f"Current page: {context.current_page or 'unknown'}\n"
                f"Selected issue: {context.selected_issue_id or 'none'}\n"
                f"Workspace: {context.workspace_scope.name} - {context.workspace_scope.description}\n"
                f"Allowed actions: {', '.join(sorted(product.allowed_actions))}"
            ),
            # Optional context, trimmed first to last when the request is too large.
            PromptSection(f"Visible issues: {', '.join(visible['issues']) or 'none'}", trim_order=1),
            PromptSection(f"Visible team members: {', '.join(visible['team']) or 'none'}", trim_order=2),
            PromptSection(f"Visible projects: {', '.join(visible['projects']) or 'none'}", trim_order=3),
            PromptSection(f"Product docs:\n{docs}\n", trim_order=4),
            PromptSection(self._action_hints()),
            PromptSection(f"Visitor message: {context.message}"),
        ]

    def _action_hints(self) -> str:
        return (
            "Action hints:\n"
            "- Weekly planning, sprint planning, time-boxed work, capacity planning -> OPEN_CYCLES.\n"
            "- Customer bugs, tickets, work items, triage, assignment -> OPEN_ISSUES or issue actions.\n"
            "- Roadmap, initiatives, project progress -> OPEN_PROJECTS.\n"
            "- People, team members, capacity, workload -> OPEN_TEAMS.\n"
            "- GitHub, Slack, PRs, commits, connected tools -> integrations actions.\n"
            "- Architecture, system design, how Pixel works -> OPEN_SYSTEM_ARCHITECTURE.\n"
            "- Salesforce, Gmail, external CRM/email -> no action; explain Pixel-only boundary.\n"
        )

    def _visible_workspace_data(
        self, workspace_scope: WorkspaceScope,
        visible_data: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, list[str]]:
        allowed_project_ids = set(workspace_scope.allowed_project_ids)
        allowed_issue_projects = set(workspace_scope.allowed_issue_projects)
        if visible_data is None:
            # The model sees only the caller's own records; there is no shared default.
            raise ValueError("model context needs the caller's own records")
        data = visible_data

        projects = [
            str(project["name"])
            for project in data["projects"]
            if project.get("id") in allowed_project_ids
        ]
        team = [
            str(member["name"])
            for member in data["team"]
            if set(member.get("projectIds") or []).intersection(allowed_project_ids)
        ]
        issues = [
            f"{issue.id} {issue.title} ({issue.assignee}, {issue.project})"
            for issue in load_demo_issues(data)
            if issue_in_scope(issue, allowed_project_ids, allowed_issue_projects)
        ]

        return {"projects": projects[:8], "team": team[:8], "issues": issues[:10]}

    def _parse_response(self, response: dict[str, Any]) -> AgentReasoningResult | None:
        if not isinstance(response, dict):
            return None
        candidates = response.get("candidates")
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            return None
        content = candidates[0].get("content")
        if not isinstance(content, dict):
            return None
        parts = content.get("parts")
        if not isinstance(parts, list):
            return None
        text = "".join(
            part["text"] for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str)
            and not part.get("thought")
        )
        if not text:
            return None

        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                return None
            try:
                raw = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None

        if not isinstance(raw, dict):
            return None
        try:
            return AgentReasoningResult.model_validate(self._normalize_result(raw))
        except ValidationError:
            return None

    def _normalize_result(self, raw: dict[str, Any]) -> dict[str, Any]:
        normalized = {**raw}
        intent_trace = normalized.get("intent_trace")
        if isinstance(intent_trace, dict):
            normalized_trace = {**intent_trace}
            if normalized_trace.get("status") not in ("active", "interrupted", "denied"):
                normalized_trace["status"] = "active"

            feature = normalized_trace.get("relevant_feature")
            if isinstance(feature, str):
                normalized_trace["relevant_feature"] = self._canonical_feature(feature)

            normalized["intent_trace"] = normalized_trace
        return normalized

    def _canonical_feature(self, feature: str) -> str:
        normalized = feature.strip().lower()
        return {
            "architecture": "Architecture",
            "cycles": "Cycles",
            "cycle": "Cycles",
            "issues": "Issues",
            "issue": "Issues",
            "tickets": "Issues",
            "ticket": "Issues",
            "projects": "Projects",
            "project": "Projects",
            "teams": "Teams",
            "team": "Teams",
            "integrations": "Integrations",
            "integration": "Integrations",
            "voice": "Voice",
        }.get(normalized, feature)

    def _default_transport(
        self,
        api_key: str,
        payload: dict[str, Any],
        timeout_ms: int,
    ) -> dict[str, Any]:
        response = http_client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self._model_name()}:generateContent",
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            body=json.dumps(payload).encode("utf-8"),
            timeout_seconds=timeout_ms / 1000,
        )
        if not response.ok:
            raise http_client.ProviderUnreachable(f"http_{response.status}")
        return response.json()

    def _model_name(self) -> str:
        return env_value("GEMINI_MODEL") or "gemini-3.5-flash-lite"


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: one token per three UTF-8 bytes, rounded up.

    Typical English runs near four characters per token, so this over-reserves.
    It is an estimate only; provider-reported counts are recorded separately.
    """
    return -(-len(text.encode("utf-8")) // 3)


def _is_timeout(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    return isinstance(error, urllib.error.URLError) and isinstance(error.reason, (TimeoutError, socket.timeout))


def _reported_tokens(response: Any) -> tuple[int | None, int | None]:
    """Provider-reported token usage, when present. Never an estimate."""
    metadata = response.get("usageMetadata") if isinstance(response, dict) else None
    if not isinstance(metadata, dict):
        return None, None
    prompt = metadata.get("promptTokenCount")
    candidates = metadata.get("candidatesTokenCount")
    thoughts = metadata.get("thoughtsTokenCount")
    input_tokens = prompt if isinstance(prompt, int) else None
    output_parts = [value for value in (candidates, thoughts) if isinstance(value, int)]
    return input_tokens, (sum(output_parts) if output_parts else None)
