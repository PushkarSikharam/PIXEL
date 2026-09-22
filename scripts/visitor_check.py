"""The public visitor script for a cutover (5c plan, section 11.3, step 3).

Runs the visitor workflow against a deployed API exactly as the public web app does, as one
private demo visitor, and prints PASS or FAIL for every check. Exit status 0 only when every
check passes.

    python scripts/visitor_check.py --api https://<api-host> --expect-authority legacy
    python scripts/visitor_check.py --api https://<api-host> --expect-authority definition

Safe to run against production:
- It signs in as a new public visitor, whose records are a private synthetic copy; the one change
  it makes (assigning that copy's LIN-142 to Noah) is undone by resetting that copy at the end.
- Every session it opens is named `synthetic-visitor-check-...`, so its turns can be labelled as
  synthetic in the cutover evidence (5d plan, section 2.1).
- Voice playback calls the speech provider, so it runs only with `--voice`.

Standard library only; nothing is imported from the application.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

TENANT = "pixel-dev"
PRODUCT = "linear-demo"
HOME = "workspace-product-eng"
OTHER = "workspace-platform"

# (method, path, body, headers) -> (status, parsed body or raw bytes)
Transport = Callable[[str, str, Any, dict[str, str]], tuple[int, Any]]


def http_transport(base_url: str, timeout: float = 30.0) -> Transport:
    def send(method: str, path: str, body: Any, headers: dict[str, str]) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method,
                                         headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, _parse(response.read(), response.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as error:
            return error.code, _parse(error.read(), error.headers.get("Content-Type", ""))
    return send


def _parse(raw: bytes, content_type: str) -> Any:
    if "json" in content_type:
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return raw
    return raw


@dataclass
class Result:
    name: str
    passed: bool
    detail: str


@dataclass
class VisitorCheck:
    send: Transport
    expect_authority: str
    voice: bool = False
    results: list[Result] = field(default_factory=list)
    token: str = ""
    turns: int = 0

    # --- plumbing ---

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", **extra} if self.token else dict(extra)

    def _record(self, name: str, passed: bool, detail: str) -> bool:
        self.results.append(Result(name, passed, detail))
        return passed

    def _turn(self, session: str, turn_id: int, message: str, *, scope: str = HOME,
              page: str = "dashboard", selected: str | None = None) -> dict:
        self.turns += 1
        status, body = self.send("POST", "/api/turn", {
            "session_id": session, "turn_id": turn_id, "product_id": PRODUCT, "message": message,
            "input_mode": "text", "current_page": page, "workspace_scope_id": scope,
            "selected_issue_id": selected,
        }, self._headers())
        if status != 200 or not isinstance(body, dict):
            raise CheckError(f"{message!r}: HTTP {status}")
        return body

    @staticmethod
    def _action(body: dict) -> tuple[str | None, dict]:
        action = body.get("validated_action") or {}
        return action.get("type"), action.get("payload") or {}

    @staticmethod
    def _session(name: str) -> str:
        return f"synthetic-visitor-check-{name}-{uuid.uuid4().hex[:8]}"

    # --- the workflow (5c plan, section 11.3, step 3) ---

    def run(self) -> bool:
        steps = [
            ("readiness reports the expected authority", self.check_health),
            ("a public visitor session starts", self.check_visitor_session),
            ("greeting", self.check_greeting),
            ("navigation", self.check_navigation),
            ("record lookup", self.check_record_lookup),
            ("create clarification writes nothing", self.check_create_clarification),
            ("deterministic update and receipt", self.check_update_and_receipt),
            ("interruption endpoint", self.check_interruption),
            ("workspace isolation", self.check_workspace_isolation),
            ("product refusal", self.check_refusal),
            ("reset restores the private copy", self.check_reset),
        ]
        if self.voice:
            steps.append(("voice playback", self.check_voice))
        for name, step in steps:
            if step not in (self.check_health, self.check_visitor_session) and not self.token:
                self._record(name, False, "not run: no visitor session")
                continue
            try:
                step(name)
            except CheckError as error:
                self._record(name, False, str(error))
            except Exception as error:  # a crashed check is a failed check, never a pass
                self._record(name, False, f"{type(error).__name__}: {error}")
        return all(result.passed for result in self.results)

    def check_health(self, name: str) -> None:
        status, body = self.send("GET", "/health", None, {})
        authority = body.get("authority") if isinstance(body, dict) else None
        self._record(name, status == 200 and body.get("status") == "ok" and authority == self.expect_authority,
                     f"HTTP {status}, status={body.get('status') if isinstance(body, dict) else body!r}, "
                     f"authority={authority!r} (expected {self.expect_authority!r})")

    def check_visitor_session(self, name: str) -> None:
        status, body = self.send("POST", f"/api/organizations/{TENANT}/products/{PRODUCT}/visitor-sessions", None, {})
        if status == 200 and isinstance(body, dict) and body.get("token"):
            self.token = body["token"]
        self._record(name, bool(self.token), f"HTTP {status}")

    def check_greeting(self, name: str) -> None:
        body = self._turn(self._session("greeting"), 1, "hi")
        self._record(name, body.get("status") == "completed" and bool(body.get("speech"))
                     and self._action(body)[0] is None and body.get("execution") is None,
                     repr(body.get("speech")))

    def check_navigation(self, name: str) -> None:
        body = self._turn(self._session("navigation"), 1, "Show me the issues")
        self._record(name, self._action(body)[0] == "OPEN_ISSUES" and body.get("execution") is None,
                     f"{self._action(body)[0]} | {body.get('speech')!r}")

    def check_record_lookup(self, name: str) -> None:
        body = self._turn(self._session("lookup"), 1, "Open Maya's ticket")
        kind, payload = self._action(body)
        self._record(name, kind == "OPEN_DEMO_ISSUE" and "LIN-142" in json.dumps(payload),
                     f"{kind} {payload} | {body.get('speech')!r}")

    def check_create_clarification(self, name: str) -> None:
        body = self._turn(self._session("create"), 1, "Create a new ticket")
        speech = body.get("speech") or ""
        self._record(name, body.get("execution") is None and "?" in speech,
                     f"{self._action(body)[0]} execution={body.get('execution')} | {speech!r}")

    def check_update_and_receipt(self, name: str) -> None:
        session = self._session("update")
        self._turn(session, 1, "Open Maya's ticket")
        body = self._turn(session, 2, "assign it to Noah", page="issue_detail", selected="LIN-142")
        kind, payload = self._action(body)
        envelope = body.get("execution") or {}
        if kind != "UPDATE_DEMO_ISSUE" or not envelope.get("key"):
            raise CheckError(f"no keyed update proposed: {kind} {envelope} | {body.get('speech')!r}")
        changes = {key: value for key, value in payload.items() if key != "issue_id"}
        status, receipt = self.send("PATCH", "/api/demo-data/issues/LIN-142", {"changes": changes},
                                    self._headers(**{"X-Execution-Key": envelope["key"], "X-Session-Id": session}))
        record = receipt.get("record", {}) if isinstance(receipt, dict) else {}
        if status != 200 or record.get("assignee") != "Noah Patel":
            raise CheckError(f"receipt HTTP {status}: {receipt!r}")
        replay_status, _ = self.send("PATCH", "/api/demo-data/issues/LIN-142", {"changes": changes},
                                     self._headers(**{"X-Execution-Key": envelope["key"], "X-Session-Id": session}))
        changed = self._turn(session, 3, "What changed?", page="issue_detail", selected="LIN-142")
        self._record(name, replay_status == 200 and "LIN-142" in (changed.get("speech") or ""),
                     f"receipt {status}, replay {replay_status}, then {changed.get('speech')!r}")

    def check_interruption(self, name: str) -> None:
        session = self._session("interrupt")
        self._turn(session, 1, "Show me the cycles")
        status, body = self.send("POST", "/api/turn/1/cancel", {"session_id": session}, self._headers())
        # A finished turn cannot be cancelled; the endpoint must answer, and say so.
        self._record(name, status == 200 and isinstance(body, dict) and body.get("status") == "not_active",
                     f"HTTP {status}: {body!r}")

    def check_workspace_isolation(self, name: str) -> None:
        body = self._turn(self._session("isolation"), 1, "Open Maya's ticket", scope=OTHER)
        speech = body.get("speech") or ""
        self._record(name, self._action(body)[0] != "OPEN_DEMO_ISSUE" and "Maya Chen" not in speech
                     and "LIN-142" not in speech, f"{self._action(body)[0]} | {speech!r}")

    def check_refusal(self, name: str) -> None:
        body = self._turn(self._session("refusal"), 1, "Open Salesforce")
        self._record(name, body.get("status") == "denied" and self._action(body)[0] is None
                     and body.get("execution") is None, f"{body.get('status')} | {body.get('speech')!r}")

    def check_reset(self, name: str) -> None:
        # Reset starts a new private generation: the old token stops working and a new one is issued,
        # with the restored records in the same response.
        status, body = self.send("POST", "/api/demo-data/reset-mine", None, self._headers())
        body = body if isinstance(body, dict) else {}
        issues = (body.get("data") or {}).get("issues", [])
        lin142 = next((issue for issue in issues if issue.get("id") == "LIN-142"), {})
        old_token, self.token = self.token, body.get("token") or self.token
        stale_status, _ = self.send("GET", "/api/demo-data", None, {"Authorization": f"Bearer {old_token}"})
        self._record(name, status == 200 and lin142.get("assignee") == "Maya Chen"
                     and self.token != old_token and stale_status in (401, 403, 404),
                     f"reset HTTP {status}; LIN-142 assignee {lin142.get('assignee')!r}; "
                     f"old token now HTTP {stale_status}")

    def check_voice(self, name: str) -> None:
        status, body = self.send("POST", "/api/speech", {"text": "Voice check.", "product_id": PRODUCT},
                                 self._headers())
        self._record(name, status == 200 and isinstance(body, bytes) and len(body) > 0,
                     f"HTTP {status}, {len(body) if isinstance(body, bytes) else 0} audio bytes")


class CheckError(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", required=True, help="API base URL, for example https://api.example.com")
    parser.add_argument("--expect-authority", required=True, choices=("legacy", "definition"))
    parser.add_argument("--voice", action="store_true", help="also check voice playback (calls the speech provider)")
    arguments = parser.parse_args(argv)
    started = time.monotonic()
    check = VisitorCheck(http_transport(arguments.api), arguments.expect_authority, voice=arguments.voice)
    passed = check.run()
    for result in check.results:
        print(f"{'PASS' if result.passed else 'FAIL'}  {result.name}: {result.detail}")
    print(f"\n{sum(r.passed for r in check.results)}/{len(check.results)} checks passed, "
          f"{check.turns} synthetic turns, {time.monotonic() - started:.1f}s")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
