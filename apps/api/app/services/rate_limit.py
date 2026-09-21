"""Abuse limits for the public demo: login, conversation, speech, writes and reset.

Provider budgets bound what abuse can *cost*; they do nothing about how fast one visitor can hit
the demo, how many tokens they can mint, or how often shared data can be disturbed. These limits
bound the rate.

Three keys, because each covers the others' blind spots:

- **The client address** (best effort). It comes from the first `X-Forwarded-For` entry, which the
  web tier sets. Anyone calling the API directly can forge that header, so this key alone proves
  nothing — it spreads honest visitors apart so one of them cannot exhaust another's allowance.
- **The authenticated identity** (not forgeable). This limits one private visitor session.
- **The deployment** (not visitor-controlled). This is the process-wide ceiling that remains when
  someone forges client addresses and repeatedly allocates fresh visitor identities.

Limits are armed when the server starts (`arm()` in the application's startup), so a unit test
that talks to the app without starting it is not throttled by the requests of unrelated tests.
`PIXEL_RATE_LIMITS=off` disarms them explicitly; only isolated test harnesses should set it.

State is kept in this process. With more than one server replica the limits apply per replica,
which is stated rather than hidden: a shared store is needed before scaling out.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass

from fastapi import HTTPException, Request

from app.services.env import env_bool
from app.tenancy import deployment_id


@dataclass(frozen=True)
class Limit:
    requests: int
    window_seconds: float


# (route, key) -> limit. Generous enough that a real demo — even one whose visitors all appear to
# come from a handful of proxy addresses — is never throttled, and low enough to stop a flood.
DEFAULT_LIMITS: dict[tuple[str, str], Limit] = {
    ("login", "client"): Limit(30, 60),
    ("login", "deployment"): Limit(120, 60),
    ("turn", "client"): Limit(120, 60),
    ("turn", "identity"): Limit(60, 60),
    ("turn", "deployment"): Limit(600, 60),
    ("speech", "client"): Limit(60, 60),
    ("speech", "identity"): Limit(60, 60),
    ("speech", "deployment"): Limit(300, 60),
    ("write", "client"): Limit(30, 60),
    ("write", "identity"): Limit(60, 60),
    ("write", "deployment"): Limit(180, 60),
    ("reset", "client"): Limit(10, 60),
    ("reset", "identity"): Limit(5, 60),
    ("reset", "deployment"): Limit(120, 60),
}

TOO_MANY = "Too many requests. Please wait a moment and try again."


class RateLimiter:
    def __init__(self, limits: dict[tuple[str, str], Limit] | None = None, clock=time.monotonic) -> None:
        self._limits = dict(limits or DEFAULT_LIMITS)
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str, str], deque[float]] = {}
        self._armed = False

    # --- arming ---

    def arm(self) -> None:
        """Called when the server starts. `PIXEL_RATE_LIMITS=off` keeps the limits disarmed."""
        self._armed = env_bool("PIXEL_RATE_LIMITS", default=True)

    def disarm(self) -> None:
        self._armed = False

    @property
    def armed(self) -> bool:
        return self._armed

    # --- checking ---

    def retry_after(self, route: str, *, client: str | None, identity: str | None) -> float | None:
        """Count one request. Returns seconds to wait if a limit is exceeded, otherwise None.

        A refused request is not counted, so a visitor who waits the stated time gets through.
        """
        if not self._armed:
            return None
        keys = {"client": client, "identity": identity, "deployment": deployment_id()}
        with self._lock:
            now = self._clock()
            applicable: list[tuple[tuple[str, str, str], Limit]] = []
            for (limited_route, key_kind), limit in self._limits.items():
                key = keys.get(key_kind)
                if limited_route != route or not key:
                    continue
                bucket = (route, key_kind, key)
                hits = self._hits.setdefault(bucket, deque())
                while hits and hits[0] <= now - limit.window_seconds:
                    hits.popleft()
                if len(hits) >= limit.requests:
                    return max(hits[0] + limit.window_seconds - now, 0.0)
                applicable.append((bucket, limit))
            for bucket, _ in applicable:
                self._hits[bucket].append(now)
            self._forget_idle(now)
        return None

    def enforce(self, route: str, request: Request, identity: str | None = None) -> None:
        """Raise 429 with Retry-After when this request is over a limit."""
        wait = self.retry_after(route, client=client_address(request), identity=identity)
        if wait is not None:
            raise HTTPException(
                status_code=429,
                detail=TOO_MANY,
                headers={"Retry-After": str(max(1, math.ceil(wait)))},
            )

    def _forget_idle(self, now: float) -> None:
        """Drop empty buckets so memory tracks recent visitors, not every visitor ever seen."""
        longest = max((limit.window_seconds for limit in self._limits.values()), default=0.0)
        for bucket in [b for b, hits in self._hits.items() if not hits or hits[-1] <= now - longest]:
            del self._hits[bucket]


def client_address(request: Request) -> str:
    """The visitor's address as the web tier reports it. Best effort; see the module docstring."""
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    if first:
        return first
    return request.client.host if request.client else "unknown"
