import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// Every visitor message reaches /api/turn before the browser executes anything, and the browser
// never writes its own assistant reply (5c plan, sections 2.3 and 9).
const PAGE = readFileSync(join(__dirname, "..", "app", "page.tsx"), "utf8");

function sendMessageBody(): string {
  const start = PAGE.indexOf("async function sendMessage(");
  expect(start).toBeGreaterThan(-1);
  const end = PAGE.indexOf("\n  }\n", start);
  return PAGE.slice(start, end);
}

describe("backend authority over conversation", () => {
  it("has no browser-built turn response", () => {
    expect(PAGE).not.toMatch(/localTurnResponse/);
    expect(PAGE).not.toMatch(/handleLocal\w*Intent|handleScopeBoundaryIntent/);
  });

  it("sends every message to the turn API before acting", () => {
    const body = sendMessageBody();
    const send = body.indexOf("sendAgentTurn(");
    expect(send).toBeGreaterThan(-1);
    const beforeSend = body.slice(0, send);
    expect(beforeSend).not.toMatch(/runAction\(/);
    expect(beforeSend).not.toMatch(/speaker: "Agent"/);
  });

  it("refuses an assistant mutation that arrives without its execution key", () => {
    expect(sendMessageBody()).toMatch(/if \(mutation && !result\.execution\)/);
  });
});
