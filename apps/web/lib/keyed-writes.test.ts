import { afterEach, describe, expect, it, vi } from "vitest";

import { sendAgentTurn } from "@/lib/agent-api";
import { applyKeyedChange, setAuthToken, type ExecutionEnvelope } from "@/lib/product-data-api";

// Milestone 3.2 slice 5b: an assistant mutation is written only under its execution key, carrying
// exactly the bound change, and the browser reports only the backend's receipt.
const ENVELOPE: ExecutionEnvelope = {
  key: "key-1",
  session_id: "session-1",
  turn_id: 2,
  expires_at: "2026-09-21T12:10:00+00:00"
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function capture(response: Response) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return response.clone();
  }));
  setAuthToken("token");
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  setAuthToken(null);
});

describe("keyed assistant writes", () => {
  it("sends only the bound change, with the key and session and no idempotency header", async () => {
    const calls = capture(jsonResponse(200, {
      outcome: "executed", code: "applied", speech: "Updated LIN-142: assignee to Noah Patel.",
      record: { id: "LIN-142", assignee: "Noah Patel" }
    }));
    const receipt = await applyKeyedChange(
      { type: "UPDATE_DEMO_ISSUE", payload: { issue_id: "LIN-142", assignee: "Noah Patel" } },
      ENVELOPE
    );
    expect(receipt?.speech).toBe("Updated LIN-142: assignee to Noah Patel.");
    expect(calls).toHaveLength(1);
    const { url, init } = calls[0];
    expect(url).toMatch(/\/demo-data\/issues\/LIN-142$/);
    expect(init.method).toBe("PATCH");
    const headers = init.headers as Record<string, string>;
    expect(headers["X-Execution-Key"]).toBe("key-1");
    expect(headers["X-Session-Id"]).toBe("session-1");
    expect(Object.keys(headers).map((name) => name.toLowerCase())).not.toContain("idempotency-key");
    expect(JSON.parse(String(init.body))).toEqual({ changes: { assignee: "Noah Patel" } });
  });

  it("drops fields the change does not set, so the body matches the bound change", async () => {
    const calls = capture(jsonResponse(200, { outcome: "executed", code: "applied", speech: "ok", record: null }));
    await applyKeyedChange(
      { type: "UPDATE_DEMO_ISSUE", payload: { issue_id: "LIN-142", priority: "High", assignee: undefined } },
      ENVELOPE
    );
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ changes: { priority: "High" } });
  });

  it("returns the backend's refusal receipt unchanged", async () => {
    capture(jsonResponse(409, {
      outcome: "refused", code: "superseded",
      speech: "A newer request replaced this change, so it wasn't applied.", record: null
    }));
    const receipt = await applyKeyedChange(
      { type: "UPDATE_DEMO_ISSUE", payload: { issue_id: "LIN-142", priority: "High" } }, ENVELOPE
    );
    expect(receipt).toEqual({
      outcome: "refused", code: "superseded",
      speech: "A newer request replaced this change, so it wasn't applied.", record: null
    });
  });

  it("returns no receipt for a key the server does not recognize", async () => {
    capture(jsonResponse(404, { detail: "This change was not found." }));
    const receipt = await applyKeyedChange(
      { type: "UPDATE_DEMO_ISSUE", payload: { issue_id: "LIN-142", priority: "High" } }, ENVELOPE
    );
    expect(receipt).toBeNull();
  });

  it("keeps an envelope only for the conversation and turn it was issued for", async () => {
    const turn = {
      session_id: "session-1", turn_id: 2, status: "completed", speech: "I'll update LIN-142.",
      proposed_action: null, validated_action: null,
      intent_trace: { status: "active", confidence: 1 }, signals: [], retrieved_context: [],
      session_summary: { interests: [], pain_points: [] }
    };
    const input = {
      sessionId: "session-1", turnId: 2, productId: "linear-demo", message: "assign it to Noah",
      inputMode: "text" as const, currentPage: "issues", workspaceScopeId: "workspace-product-eng"
    };
    capture(jsonResponse(200, { ...turn, execution: ENVELOPE }));
    expect((await sendAgentTurn(input)).execution).toEqual(ENVELOPE);
    capture(jsonResponse(200, { ...turn, execution: { ...ENVELOPE, turn_id: 1 } }));
    expect((await sendAgentTurn(input)).execution).toBeNull();
    capture(jsonResponse(200, { ...turn, execution: null }));
    expect((await sendAgentTurn(input)).execution).toBeNull();
  });

  it("keeps a keyed create's fields exactly as bound and posts them unchanged", async () => {
    const fields = {
      title: "Login errors", priority: "Medium", assignee: "Noah Patel", project: "Issue Triage", status: "Todo"
    };
    capture(jsonResponse(200, {
      session_id: "session-1", turn_id: 2, status: "completed", speech: "I'll create a ticket.",
      proposed_action: { type: "CREATE_DEMO_ISSUE", payload: fields },
      validated_action: { type: "CREATE_DEMO_ISSUE", payload: fields },
      intent_trace: { status: "active", confidence: 1 }, signals: [], retrieved_context: [],
      session_summary: { interests: [], pain_points: [] }, execution: ENVELOPE
    }));
    const turn = await sendAgentTurn({
      sessionId: "session-1", turnId: 2, productId: "linear-demo", message: "create a ticket for Noah",
      inputMode: "text", currentPage: "issues", workspaceScopeId: "workspace-product-eng"
    });
    // No ID is invented: the server assigns it when the create commits.
    expect(turn.validated_action).toEqual({ type: "CREATE_DEMO_ISSUE", payload: fields });

    const calls = capture(jsonResponse(200, {
      outcome: "executed", code: "applied", speech: "Created PIX-143: ...", record: { id: "PIX-143", ...fields }
    }));
    if (turn.validated_action?.type !== "CREATE_DEMO_ISSUE") throw new Error("expected a create");
    await applyKeyedChange(turn.validated_action, ENVELOPE);
    expect(calls[0].init.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ fields });
  });
});
