// Live smoke test for a deployed Pixel. It makes no paid call unless PIXEL_SMOKE_SPEECH=true.
//
// PIXEL_LIVE_URL       Deployed web origin; requests use its /api/agent proxy.
// PIXEL_SMOKE_API_URL Direct API /api base. Plain HTTP is accepted only on this machine.

const liveUrl = (process.env.PIXEL_LIVE_URL ?? "").replace(/\/$/, "");
const directApiUrl = (process.env.PIXEL_SMOKE_API_URL ?? "").replace(/\/$/, "");

function apiBase() {
  if (directApiUrl) {
    const { protocol, hostname } = new URL(directApiUrl);
    if (protocol !== "https:" && hostname !== "127.0.0.1" && hostname !== "localhost") {
      throw new Error("PIXEL_SMOKE_API_URL must use HTTPS unless it points at this machine.");
    }
    return directApiUrl;
  }
  if (liveUrl.startsWith("https://")) return `${liveUrl}/api/agent`;
  throw new Error(
    "Set PIXEL_LIVE_URL to the deployed HTTPS web origin, or PIXEL_SMOKE_API_URL to an API's /api base."
  );
}

const base = apiBase();

async function call(path, init = {}) {
  return fetch(`${base}${path}`, init);
}

async function request(path, init = {}) {
  const response = await call(path, init);
  if (!response.ok) {
    throw new Error(`${init.method ?? "GET"} ${path} returned ${response.status}: ${await response.text()}`);
  }
  return response;
}

function turnBody(sessionId, turnId, message) {
  return JSON.stringify({
    session_id: sessionId,
    turn_id: turnId,
    product_id: "linear-demo",
    message,
    input_mode: "text",
    current_page: "dashboard",
    workspace_scope_id: "workspace-product-eng"
  });
}

async function startVisitor() {
  return (await request("/organizations/pixel-dev/products/linear-demo/visitor-sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" }
  })).json();
}

const health = await (await request("/health")).json();
if (health.status !== "ok") throw new Error("The API did not report ready.");

const first = await startVisitor();
const second = await startVisitor();
if (first.visitor_id === second.visitor_id || first.instance_id === second.instance_id) {
  throw new Error("Two public visitors received the same identity or private instance.");
}
const firstAuthorization = { Authorization: `Bearer ${first.token}` };
const secondAuthorization = { Authorization: `Bearer ${second.token}` };

const adminAttempt = await call("/auth/demo-login", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ user_id: "demo-admin" })
});
if (adminAttempt.ok) throw new Error("The public demo login issued an administrator token.");

const firstData = await (await request("/demo-data", { headers: firstAuthorization })).json();
const secondData = await (await request("/demo-data", { headers: secondAuthorization })).json();
if (!Array.isArray(firstData.issues) || !Array.isArray(firstData.workspaceScopes)
    || firstData.issues.length === 0) {
  throw new Error("The private demo-data response is incomplete.");
}
if (JSON.stringify(firstData) !== JSON.stringify(secondData)) {
  throw new Error("Fresh private instances did not start from identical seed data.");
}

const maya = firstData.issues.find((issue) => issue.id === "LIN-142");
if (!maya || typeof maya.revision !== "number") {
  throw new Error("The private seed has no revisioned LIN-142.");
}
await request("/demo-data/issues/LIN-142", {
  method: "PUT",
  headers: { ...firstAuthorization, "Content-Type": "application/json" },
  body: JSON.stringify({ ...maya, assignee: "Noah Patel" })
});
const firstChanged = await (await request("/demo-data", { headers: firstAuthorization })).json();
const secondUnchanged = await (await request("/demo-data", { headers: secondAuthorization })).json();
if (firstChanged.issues.find((issue) => issue.id === "LIN-142")?.assignee !== "Noah Patel"
    || secondUnchanged.issues.find((issue) => issue.id === "LIN-142")?.assignee !== "Maya Chen") {
  throw new Error("A private write crossed the visitor-instance boundary.");
}

const sessionId = crypto.randomUUID();
const opened = await (await request("/turn", {
  method: "POST",
  headers: { ...secondAuthorization, "Content-Type": "application/json" },
  body: turnBody(sessionId, 1, "Show sprint planning")
})).json();
if (opened.status !== "completed" || opened.validated_action?.type !== "OPEN_CYCLES") {
  throw new Error(
    `A working request was not answered: status=${opened.status}, `
    + `reason=${opened.intent_trace?.reason ?? "none"}`
  );
}

const refused = await (await request("/turn", {
  method: "POST",
  headers: { ...secondAuthorization, "Content-Type": "application/json" },
  body: turnBody(sessionId, 2, "Open Salesforce")
})).json();
const refusalReason = refused.intent_trace?.reason ?? "";
if (refused.status !== "denied" || refused.validated_action !== null
    || !refusalReason.includes("outside this product demo")) {
  throw new Error(`The guardrail did not refuse for its own reason: ${refusalReason || "none"}`);
}

const globalReset = await call("/demo-data/reset", { method: "POST", headers: firstAuthorization });
if (globalReset.status !== 403) {
  throw new Error(`A visitor reached the global reset (status ${globalReset.status}).`);
}
const restored = await (await request("/demo-data/reset-mine", {
  method: "POST",
  headers: firstAuthorization
})).json();
if (restored.generation !== first.generation + 1
    || restored.data.issues.find((issue) => issue.id === "LIN-142")?.assignee !== "Maya Chen") {
  throw new Error("Private reset did not restore the seed and rotate the generation.");
}
if ((await call("/demo-data", { headers: firstAuthorization })).status !== 401) {
  throw new Error("The token for the reset generation remained active.");
}

let speech = "not requested";
if (process.env.PIXEL_SMOKE_SPEECH === "true") {
  const response = await request("/speech", {
    method: "POST",
    headers: { ...secondAuthorization, "Content-Type": "application/json" },
    body: JSON.stringify({
      text: "Pixel deployment check.",
      product_id: "linear-demo",
      session_id: sessionId
    })
  });
  speech = `${response.headers.get("x-tts-engine") ?? "unknown"} (${(await response.arrayBuffer()).byteLength} bytes)`;
}

console.log(JSON.stringify({
  target: base,
  health: health.status,
  visitor_instances: "separate",
  administrator_login: "refused",
  issues: firstData.issues.length,
  workspaces: firstData.workspaceScopes.length,
  private_write: "isolated",
  private_reset: `generation ${restored.generation}`,
  conversation: `${opened.status} (${opened.validated_action.type})`,
  guardrail: refused.status,
  global_reset: "refused",
  speech
}, null, 2));
