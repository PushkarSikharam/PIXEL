"""The optional model gateway (5c plan, section 8): production-disabled, budgeted, one attempt.

Cutover never depends on a paid model. The gateway is used only when a deployment switches it on
with `PIXEL_MODEL_GATEWAY=on` *and* supplies a transport; no transport is configured by default,
so it is off everywhere unless an operator deliberately enables it in a separate reviewed change.

When it runs:

- only for a turn the deterministic router could not handle (fallback), never for a confirmation;
- once, with no hidden retry, after the global paid-provider switch and the usage ledger's budget
  have both allowed the attempt;
- its reply goes through the unchanged 4a chain (strict parser, provenance, validation, forced
  confirmation for any model-originated mutation) in `app.engine.model_turn`;
- it never supplies speech: every sentence is still composed by the platform.

Every outcome is named and deterministic: disabled, blocked, over_budget, timeout, failed or reply.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Protocol
from uuid import uuid4

from app.tenancy import ProductContext
from app.services.env import env_value
from app.services.provider_policy import paid_providers_enabled
from app.services.usage_ledger import AccountingUnavailable, AttemptRequest, BudgetExceeded, UsageLedger

# A turn's single attempt may not run longer than this.
TIMEOUT_MS = 6000
MAX_PROMPT_CHARS = 60_000


class ModelTransport(Protocol):
    """Sends one prompt and returns (raw reply text, input tokens or None, output tokens or None)."""

    model: str

    def __call__(self, prompt: str, timeout_ms: int) -> tuple[str, int | None, int | None]: ...


@dataclass(frozen=True)
class GatewayResult:
    outcome: str  # disabled | blocked | over_budget | timeout | failed | reply
    raw: str | None = None


class ModelGateway:
    def __init__(self, transport: ModelTransport | None = None, usage: UsageLedger | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._transport = transport
        self._usage = usage or UsageLedger()
        self._clock = clock

    def enabled(self) -> bool:
        return self._transport is not None and (env_value("PIXEL_MODEL_GATEWAY") or "").strip().lower() == "on"

    def attempt(self, prompt: str, *, owner: ProductContext, user_id: str, session_id: str) -> GatewayResult:
        if not self.enabled():
            return GatewayResult("disabled")
        # The global kill switch is checked before any budget is reserved.
        if not paid_providers_enabled(owner):
            return GatewayResult("blocked")
        if len(prompt) > MAX_PROMPT_CHARS:
            return GatewayResult("blocked")
        try:
            attempt_id = self._usage.reserve(AttemptRequest(
                tenant=owner, user_id=user_id, request_id=str(uuid4()), capability="reasoning",
                provider="model_gateway", model=getattr(self._transport, "model", None),
                session_id=session_id, reserved_units=len(prompt) // 4 + 1_000,
            ))
        except BudgetExceeded:
            return GatewayResult("over_budget")
        except AccountingUnavailable:
            return GatewayResult("blocked")
        started = self._clock()
        try:
            raw, input_tokens, output_tokens = self._transport(prompt, TIMEOUT_MS)  # type: ignore[misc]
        except TimeoutError:
            self._settle(attempt_id, owner, "timeout", started)
            return GatewayResult("timeout")
        except Exception as error:  # noqa: BLE001 - any provider failure is one named outcome
            self._settle(attempt_id, owner, "failed", started, type(error).__name__)
            return GatewayResult("failed")
        self._settle(attempt_id, owner, "succeeded", started, input_tokens=input_tokens,
                     output_tokens=output_tokens)
        return GatewayResult("reply", raw if isinstance(raw, str) else "")

    def _settle(self, attempt_id: str, owner: ProductContext, status: str, started: float,
                reason: str | None = None, input_tokens: int | None = None, output_tokens: int | None = None) -> None:
        reported = [value for value in (input_tokens, output_tokens) if value is not None]
        try:
            self._usage.settle(
                attempt_id, owner, status, duration_ms=int((self._clock() - started) * 1000),
                actual_units=sum(reported) if reported else None, actual_input_units=input_tokens,
                actual_output_units=output_tokens, reason=reason,
            )
        except Exception:  # noqa: BLE001 - the reservation simply stays consumed
            pass
