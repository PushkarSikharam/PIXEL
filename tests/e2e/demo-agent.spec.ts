import { expect, test } from "@playwright/test";
import {
  agentApiRoute,
  agentCancelTurnOneRoute,
  agentTurnRoute,
  apiPort,
  browserAuthHeaders,
  delay,
  escapedRequests,
  forwardToFreshBackend,
  freshBackendUrl,
  installMockVoice,
  openApp,
  sendChat,
  setupIsolatedApp
} from "./harness";

setupIsolatedApp();

const definitionAuthority = process.env.PIXEL_ENGINE_MODE === "definition";
// The sentence each authority speaks for the same behaviour. Definition wording is the platform's
// (reviewed in golden/browser_differences_definition.json); every behaviour assertion is shared.
const said = (legacy: string, definition: string) => (definitionAuthority ? definition : legacy);

test("live readiness blocks partial demo behavior and recovers cleanly", async ({ page }) => {
  let loginRequests = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.endsWith("/visitor-sessions")) loginRequests += 1;
  });
  await page.route("**/api/agent/health", (route) => route.fulfill({
    status: 503,
    contentType: "application/json",
    body: JSON.stringify({ status: "unavailable" })
  }));

  await page.goto("/");
  await expect(page.getByTestId("service-readiness")).toContainText("Service unavailable");
  await expect(page.getByTestId("chat-input")).toBeDisabled();
  await expect(page.getByTestId("chat-send")).toBeDisabled();
  await expect(page.getByTestId("voice-toggle")).toBeDisabled();
  await expect(page.getByTestId("demo-path-1")).toBeDisabled();
  expect(loginRequests, "an unhealthy service must not receive a login attempt").toBe(0);
  await expect(page.getByTestId("transcript")).not.toContainText("backend is running");

  await page.unroute("**/api/agent/health");
  const dataLoaded = page.waitForResponse(
    (response) => new URL(response.url()).pathname === "/api/agent/demo-data" && response.ok()
  );
  await page.getByTestId("service-readiness").getByRole("button", { name: "Retry" }).click();
  await dataLoaded;
  await expect(page.getByTestId("service-readiness")).toBeHidden();
  await expect(page.getByTestId("chat-input")).toBeEnabled();
});

test("phase 1 creates a ticket through the form with the selected owner", async ({ page }) => {
  await openApp(page);
  await page.getByTestId("nav-issues").click();
  await page.getByTestId("create-ticket-button").click();
  await page.getByTestId("ticket-title-input").fill("Audit form-created ticket");
  await page.getByTestId("ticket-assignee-select").selectOption("Noah Patel");
  const saved = page.waitForResponse((response) =>
    response.url().endsWith("/demo-data/issues") && response.request().method() === "POST"
  );
  await page.getByTestId("submit-create-ticket").click();
  expect((await saved).ok()).toBe(true);
  await expect(page.getByTestId("issue-detail-panel")).toContainText("Audit form-created ticket");
  await page.reload();
  await page.getByTestId("nav-issues").click();
  await expect(page.getByText("Audit form-created ticket", { exact: true })).toBeVisible();
  const data = await (await fetch(`http://127.0.0.1:${apiPort}/api/demo-data`, {
    headers: await browserAuthHeaders(page)
  })).json();
  expect(data.issues.find((issue: { title: string }) => issue.title === "Audit form-created ticket").assignee)
    .toBe("Noah Patel");
});

for (const entity of ["project", "cycle"] as const) {
  test(`phase 1 creates and reloads a ${entity} without changing existing records`, async ({ page }) => {
    await openApp(page);
    const headers = await browserAuthHeaders(page);
    const before = await (await fetch(`http://127.0.0.1:${apiPort}/api/demo-data`, { headers })).json();
    await page.getByTestId(`nav-${entity}s`).click();
    await page.getByTestId(`create-${entity}-button`).click();
    const name = `Audit ${entity}`;
    await page.getByTestId(`${entity}-name-input`).fill(name);
    const saved = page.waitForResponse((response) =>
      response.url().includes(`/demo-data/${entity}s`) && response.request().method() === "POST"
    );
    await page.getByTestId(`submit-create-${entity}`).click();
    expect((await saved).ok()).toBe(true);
    await expect(page.getByText(name, { exact: true })).toBeVisible();
    await page.reload();
    await page.getByTestId(`nav-${entity}s`).click();
    await expect(page.getByText(name, { exact: true })).toBeVisible();
    const after = await (await fetch(`http://127.0.0.1:${apiPort}/api/demo-data`, { headers })).json();
    for (const previous of before[`${entity}s`]) {
      expect(after[`${entity}s`].find((record: { id: string }) => record.id === previous.id)).toEqual(previous);
    }
  });
}

test("phase 1 failed saves do not report success", async ({ page }) => {
  await openApp(page);
  await page.getByTestId("nav-projects").click();
  await page.getByTestId("create-project-button").click();
  await page.getByTestId("project-name-input").fill("Unsaved audit project");
  await page.route("**/api/agent/demo-data/projects?**", (route) => route.fulfill({
    status: 503, contentType: "application/json", body: JSON.stringify({ error: "Unavailable" })
  }));
  const saved = page.waitForResponse((response) =>
    response.url().includes("/demo-data/projects") && response.request().method() === "POST"
  );
  await page.getByTestId("submit-create-project").click();
  expect((await saved).status()).toBe(503);
  await expect(page.getByTestId("project-create-panel")).toBeVisible({ timeout: 1000 });
  await expect(page.getByTestId("project-create-panel").getByRole("alert")).toContainText("Could not save");
  await expect(page.getByTestId("project-name-input")).toHaveValue("Unsaved audit project");
  await page.unroute("**/api/agent/demo-data/projects?**");
  const retried = page.waitForResponse((response) =>
    response.url().includes("/demo-data/projects") && response.request().method() === "POST"
  );
  await page.getByTestId("submit-create-project").click();
  expect((await retried).ok()).toBe(true);
  await expect(page.getByTestId("project-create-panel")).toBeHidden();
});

test("milestone 1 failed reset keeps the session and reports the failure", async ({ page }) => {
  await openApp(page);
  await sendChat(page, "all tickets for Maya");
  await expect(page.getByTestId("issue-filter")).toContainText("Maya Chen");

  // Reset is one private transaction; make that transaction fail.
  await page.route("**/api/agent/demo-data/reset-mine", (route) => route.fulfill({
    status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Unavailable" })
  }));
  const reset = page.waitForResponse((response) => response.url().endsWith("/api/agent/demo-data/reset-mine"));
  await page.getByTestId("reset-demo").click();
  await page.getByTestId("reset-demo-confirm").click();
  expect((await reset).status()).toBe(503);

  await expect(page.getByTestId("data-error")).toContainText("Reset failed");
  await expect(page.getByTestId("issue-filter")).toContainText("Maya Chen");
  await expect(page.getByTestId("transcript")).toContainText("Maya Chen");
});

test("a throttled turn keeps the demo connected and asks the visitor to wait", async ({ page }) => {
  await openApp(page);
  await page.route(agentTurnRoute, (route) => route.fulfill({
    status: 429,
    contentType: "application/json",
    headers: { "Retry-After": "5" },
    body: JSON.stringify({ detail: "Too many requests. Please wait a moment and try again." })
  }));

  await sendChat(page, "show sprint planning");

  await expect(page.getByTestId("transcript")).toContainText("Please wait a moment and try again.");
  await expect(page.getByTestId("turn-status")).toHaveText("Ready");
  await expect(page.getByTestId("service-readiness")).toBeHidden();
  await expect(page.getByTestId("current-view-title")).toHaveText("Dashboard");

  // Once the limit passes, the same conversation carries on.
  await page.unroute(agentTurnRoute);
  await sendChat(page, "show sprint planning");
  await expect(page.getByTestId("current-view-title")).toHaveText("Cycles");
});

test("a throttled sign-in says to wait rather than reporting an outage", async ({ page }) => {
  await page.route("**/api/agent/organizations/*/products/*/visitor-sessions", (route) => route.fulfill({
    status: 429,
    contentType: "application/json",
    headers: { "Retry-After": "5" },
    body: JSON.stringify({ detail: "Too many requests. Please wait a moment and try again." })
  }));
  await page.goto("/");

  await expect(page.getByTestId("service-readiness")).toContainText("Too many visitors are starting sessions");
  await expect(page.getByTestId("service-readiness").getByRole("button", { name: "Retry" })).toBeVisible();
});

test("milestone 1 requests that escape interception never reach a real backend", async ({ request }) => {
  // APIRequestContext bypasses browser routing without removing a live app's protection.
  const response = await request.get("/api/agent/demo-data");
  // Only the sentinel answers 503; the development backend would have served data.
  expect(response.status()).toBe(503);
  expect(escapedRequests.splice(0)).toEqual(["GET /api/demo-data"]);
});

test("milestone 1 removing a page override preserves backend isolation", async ({ page }) => {
  await openApp(page);
  const dataRoute = "**/api/agent/demo-data";
  await page.route(dataRoute, (route) => route.fulfill({ status: 503, body: "Unavailable" }));
  expect(await page.evaluate(async () => (await fetch("/api/agent/demo-data")).status)).toBe(503);
  await page.unroute(dataRoute);
  const status = await page.evaluate(async () => {
    const key = Object.keys(window.sessionStorage)
      .find((candidate) => candidate.startsWith("pixel_demo_auth:"));
    const token = key ? window.sessionStorage.getItem(key) : null;
    return (await fetch("/api/agent/demo-data", {
      headers: { Authorization: `Bearer ${token}` }
    })).status;
  });
  expect(status).toBe(200);
  expect(escapedRequests).toEqual([]);
});

test("milestone 2 speech is only requested after the visitor turns voice on", async ({ page }) => {
  const speechRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/agent/speech") speechRequests.push(request.method());
  });

  await openApp(page);
  await sendChat(page, "show me the projects");
  await page.waitForLoadState("networkidle");
  expect(speechRequests, "No greeting, prewarming or reply speech without consent").toEqual([]);

  await page.getByTestId("tts-toggle").click();
  const speech = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/agent/speech");
  await sendChat(page, "show me the cycles");
  const refused = await speech;
  // The test API has paid providers switched off, so the API refuses before any dispatch.
  expect(refused.status()).toBe(429);
  expect((await refused.json()).reason).toBe("providers_disabled");
  expect(speechRequests).toEqual(["POST"]);
  await expect(page.getByTestId("voice-fallback")).toHaveText(
    "Cloud voice unavailable. Using browser voice."
  );
});

for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
  test(`phase 1 layout evidence at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await openApp(page);
    await page.screenshot({ path: testInfo.outputPath("dashboard.png"), fullPage: true });
    await expect(page.getByTestId("chat-input")).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await page.getByTestId("nav-issues").click();
    await page.getByTestId("create-ticket-button").click();
    await page.screenshot({ path: testInfo.outputPath("issue-form.png"), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  });
}

test("keeps the core demo controls visible and navigates every product area", async ({ page }) => {
  await openApp(page);

  const navCases = [
    ["nav-issues", "Issues"],
    ["nav-projects", "Projects"],
    ["nav-cycles", "Cycles"],
    ["nav-teams", "Teams"],
    ["nav-integrations", "Integrations"],
    ["nav-dashboard", "Dashboard"]
  ] as const;

  for (const [testId, title] of navCases) {
    await page.getByTestId(testId).click();
    await expect(page.getByTestId("current-view-title")).toHaveText(title);
    if (title === "Issues") {
      await expect(page.getByTestId("create-ticket-button")).toBeVisible();
    }
  }
});

test("shows only the current workspace scope across product views", async ({ page }) => {
  await openApp(page);

  await expect(page.getByTestId("workspace-scope-badge")).toHaveText("2 scoped projects");
  await expect(page.getByText("LIN-142 - Maya Chen - Integrations")).toBeVisible();
  await expect(page.getByText("LIN-131 - Avery Brooks - Planning")).toBeHidden();

  await page.getByTestId("nav-issues").click();
  await expect(page.getByTestId("issue-count-badge")).toHaveText("3 open");
  await expect(page.getByText("LIN-137 - Noah Patel - Issue Triage")).toBeVisible();
  await expect(page.getByText("LIN-131 - Avery Brooks - Planning")).toBeHidden();

  await page.getByTestId("nav-projects").click();
  await expect(page.getByText("GitHub Integration Hardening")).toBeVisible();
  await expect(page.getByText("Issue Triage Workflow")).toBeVisible();
  await expect(page.getByText("Cycle Planning Insights")).toBeHidden();

  await page.getByTestId("nav-teams").click();
  await expect(page.getByTestId("team-member-count")).toHaveText("2 members");
  await expect(page.getByText("Maya Chen")).toBeVisible();
  await expect(page.getByText("Avery Brooks")).toBeHidden();
});

test("milestone 1 duplicate project names cannot widen the browser workspace", async ({ page }) => {
  await openApp(page);
  const created = await fetch(
    `http://127.0.0.1:${apiPort}/api/demo-data/projects?workspace_scope_id=workspace-product-eng`,
    {
      method: "POST",
      headers: { ...(await browserAuthHeaders(page)), "Content-Type": "application/json" },
      body: JSON.stringify({
        name: "Planning", description: "Name collision regression", progress: 0,
        status: "Planned", lead: "Maya Chen", team: "Product Engineering", targetDate: "2026-12-01"
      })
    }
  );
  expect(created.ok).toBe(true);
  await page.reload();
  await expect(page.getByTestId("workspace-scope-badge")).toHaveText("3 scoped projects");
  await page.getByTestId("nav-issues").click();
  await expect(page.getByTestId("issue-count-badge")).toHaveText("3 open");
  await expect(page.getByText("LIN-131 - Avery Brooks - Planning")).toBeHidden();
  await page.getByTestId("nav-teams").click();
  await expect(page.getByTestId("team-member-count")).toHaveText("2 members");
  await page.getByTestId("workspace-switcher").selectOption("workspace-platform");
  await page.getByTestId("nav-issues").click();
  await expect(page.getByText("LIN-131 - Avery Brooks - Planning")).toBeVisible();
});

test("switches workspace scope and updates product data boundaries", async ({ page }) => {
  await openApp(page);

  await page.getByTestId("workspace-switcher").selectOption("workspace-platform");

  await expect(page.getByRole("heading", { name: "Platform Workspace" })).toBeVisible();
  await expect(page.getByTestId("workspace-scope-badge")).toHaveText("2 scoped projects");
  await expect(page.getByText("LIN-131 - Avery Brooks - Planning")).toBeVisible();
  await expect(page.getByText("LIN-142 - Maya Chen - Integrations")).toBeHidden();
  await expect(page.getByText("Platform Cycle 21")).toBeVisible();

  await page.getByTestId("nav-projects").click();
  await expect(page.getByText("Cycle Planning Insights")).toBeVisible();
  await expect(page.getByText("Workspace Migration")).toBeVisible();
  await expect(page.getByText("GitHub Integration Hardening")).toBeHidden();

  await page.getByTestId("nav-teams").click();
  await expect(page.getByTestId("team-member-count")).toHaveText("2 members");
  await expect(page.getByText("Avery Brooks")).toBeVisible();
  await expect(page.getByText("Maya Chen")).toBeHidden();
});

test("keeps Edith inside the selected workspace scope", async ({ page }) => {
  await openApp(page);

  await page.getByTestId("workspace-switcher").selectOption("workspace-platform");
  await sendChat(page, "how many team members are there");
  await expect(page.getByTestId("current-view-title")).toHaveText("Teams");
  await expect(page.getByTestId("transcript")).toContainText(
    said("There are 2 team members in Platform Workspace", "Platform Workspace has 2 team members.")
  );

  // Security (5c plan, section 3.3): someone in another workspace reads exactly like an unknown
  // person; their ticket never opens and their name is never confirmed.
  await sendChat(page, "open Maya's ticket");
  await expect(page.getByTestId("transcript")).toContainText(
    // An unknown name gets the identical reply, so nothing is revealed (API security tests).
    said("I could not find a ticket for Maya", "I can't find Maya in this workspace. I'll open Issues.")
  );
  await expect(page.getByTestId("transcript")).not.toContainText("Maya Chen");
  await expect(page.getByTestId("selected-issue-id")).toHaveCount(0);

  await sendChat(page, "open Avery's ticket");
  await expect(page.getByTestId("current-view-title")).toHaveText("Issue Detail");
  await expect(page.getByTestId("selected-issue-id")).toHaveText("LIN-131");
});

test("sends the selected workspace scope to the agent API", async ({ page }) => {
  let capturedWorkspaceScopeId = "";
  let markTurnHandled!: () => void;
  const turnHandled = new Promise<void>((resolve) => {
    markTurnHandled = resolve;
  });

  await page.route(agentTurnRoute, async (route) => {
    const body = route.request().postDataJSON();
    capturedWorkspaceScopeId = body.workspace_scope_id;
    const response = await route.fetch({
      url: freshBackendUrl(route.request().url())
    });
    await route.fulfill({ response });
    markTurnHandled();
  });

  await openApp(page);
  await page.getByTestId("workspace-switcher").selectOption("workspace-platform");
  await sendChat(page, "show sprint planning");
  await turnHandled;

  expect(capturedWorkspaceScopeId).toBe("workspace-platform");
});

test("never reveals or creates work for people outside the current workspace", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "create a ticket for Avery");

  // Avery works in another workspace, so here Avery reads like anyone unknown: nothing is created
  // and nothing confirms Avery exists elsewhere.
  await expect(page.getByTestId("transcript")).toContainText(
    said("Avery is not in the team directory yet", "I can't find Avery in this workspace.")
  );
  await expect(page.getByTestId("transcript")).not.toContainText("Avery Brooks");
  await expect(page.getByTestId("transcript")).not.toContainText("Created");
});

test("collapses and expands the assistant sidebar", async ({ page }) => {
  await openApp(page);

  await page.getByTestId("assistant-collapse").click();

  await expect(page.getByTestId("assistant-expand")).toBeVisible();
  await expect(page.getByTestId("chat-input")).toBeHidden();

  await page.getByTestId("assistant-expand").click();

  await expect(page.getByTestId("chat-input")).toBeVisible();
});

test("keeps assistant controls contained in a narrow desktop sidebar", async ({ page }) => {
  await page.setViewportSize({ width: 920, height: 860 });
  await openApp(page);

  const panelBox = await page.locator(".assistant-panel").boundingBox();
  expect(panelBox).not.toBeNull();

  const checkedSelectors = [
    "[data-testid='demo-path']",
    ".demo-prompt-strip",
    ".chat-form",
    ".voice-control-card"
  ];

  for (const selector of checkedSelectors) {
    const childBox = await page.locator(selector).boundingBox();
    expect(childBox).not.toBeNull();
    expect(childBox!.x).toBeGreaterThanOrEqual(panelBox!.x);
    expect(childBox!.x + childBox!.width).toBeLessThanOrEqual(panelBox!.x + panelBox!.width + 1);
  }
});

test("keeps the assistant voice controls visible on the first desktop viewport", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openApp(page);

  const voiceBox = await page.locator(".voice-control-card").boundingBox();
  expect(voiceBox).not.toBeNull();
  expect(voiceBox!.y + voiceBox!.height).toBeLessThanOrEqual(900);
});

test("opens the sprint planning view from chat and shows retrieved product context", async ({
  page
}) => {
  await openApp(page);

  await sendChat(page, "show sprint planning");

  await expect(page.getByTestId("current-view-title")).toHaveText("Cycles");
  await expect(page.getByTestId("source-list")).toBeHidden();
  await expect(page.getByTestId("transcript")).toContainText(
    said("I'll show you the current cycle.", "I'll open Cycles.")
  );
});

test("opens the system architecture from Edith chat", async ({ page }) => {
  await openApp(page);

  await page.getByTestId("chat-input").fill("open the system architecture");
  await page.getByTestId("chat-send").click();

  await expect(page).toHaveURL(/\/architecture$/);
  await expect(page.getByRole("heading", {
    name: "How Pixel turns conversation into scoped product action."
  })).toBeVisible();
});

test("runs suggested demo turns and resets to a fresh session", async ({ page }) => {
  await openApp(page);

  await page.getByTestId("demo-prompt-back-to-github-integrations").click();
  await expect(page.getByTestId("turn-status")).not.toHaveText("Thinking");
  await expect(page.getByTestId("current-view-title")).toHaveText("Integrations");
  await expect(page.getByTestId("github-integration-card")).toHaveClass(/highlighted-card/);

  await sendChat(page, "all tickets for Maya");
  await expect(page.getByTestId("issue-filter")).toContainText("Maya Chen");

  await page.getByTestId("reset-demo").click();
  await page.getByTestId("reset-demo-confirm").click();

  await expect(page.getByTestId("current-view-title")).toHaveText("Dashboard");
  await expect(page.getByTestId("turn-status")).toHaveText("Ready");
  await expect(page.getByTestId("transcript")).toContainText("Welcome to Pixel");
  await expect(page.getByTestId("transcript")).not.toContainText("Maya Chen");
  await expect(page.getByTestId("source-list")).toBeHidden();
});

test("reset returns to the default workspace, where the guided prompts apply", async ({ page }) => {
  // The public visitor sees both workspaces, and the platform lists Platform first. Reset used to
  // land there, so "Open Maya's ticket" failed after every reset on the live demo.
  await openApp(page);
  await expect(page.getByTestId("workspace-switcher")).toHaveValue("workspace-product-eng");
  await page.getByTestId("workspace-switcher").selectOption("workspace-platform");
  await expect(page.getByTestId("workspace-switcher")).toHaveValue("workspace-platform");

  await page.getByTestId("reset-demo").click();
  await page.getByTestId("reset-demo-confirm").click();
  await expect(page.getByTestId("turn-status")).toHaveText("Ready");
  await expect(page.getByTestId("workspace-switcher")).toHaveValue("workspace-product-eng");

  await sendChat(page, "open ticket for maya");
  await expect(page.getByTestId("selected-issue-id")).toHaveText("LIN-142");
});

test("reset restores only the current visitor's private demo", async ({ page }) => {
  const resets: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/demo-data/reset")) resets.push(new URL(request.url()).pathname);
  });
  await openApp(page);

  const restored = page.waitForResponse((response) =>
    response.url().endsWith("/api/agent/demo-data/reset-mine")
    && response.request().method() === "POST"
  );
  await page.getByTestId("reset-demo").click();
  await page.getByTestId("reset-demo-confirm").click();
  expect((await restored).ok()).toBe(true);
  await expect(page.getByTestId("turn-status")).toHaveText("Ready");
  expect(resets).toEqual(["/api/agent/demo-data/reset-mine"]);

  // The API refuses the global reset for the public visitor even when asked directly.
  const status = await page.evaluate(async () => {
    const key = Object.keys(window.sessionStorage)
      .find((candidate) => candidate.startsWith("pixel_demo_auth:"));
    const token = key ? window.sessionStorage.getItem(key) : null;
    const response = await fetch("/api/agent/demo-data/reset", {
      method: "POST",
      headers: token ? { Authorization: `Bearer ${token}` } : {}
    });
    return response.status;
  });
  expect(status).toBe(403);
});

test("restart preserves private records while reset restores this visitor's seed", async ({ page }) => {
  await openApp(page);
  await sendChat(page, "open ticket for maya");

  const saved = page.waitForResponse((response) =>
    response.url().endsWith("/demo-data/issues/LIN-142")
    && response.request().method() === "PUT"
  );
  await page.getByTestId("assignee-select").selectOption("Noah Patel");
  expect((await saved).ok()).toBe(true);
  await expect(page.getByTestId("assignee-select")).toHaveValue("Noah Patel");

  await page.getByTestId("restart-chat").click();
  await expect(page.getByTestId("current-view-title")).toHaveText("Dashboard");
  await expect(page.getByTestId("transcript")).toContainText("Welcome to Pixel");
  await page.getByTestId("nav-issues").click();
  await page.getByRole("button", { name: "Open LIN-142" }).click();
  await expect(page.getByTestId("assignee-select")).toHaveValue("Noah Patel");

  await page.getByTestId("reset-demo").click();
  await page.getByTestId("reset-demo-confirm").click();
  await expect(page.getByTestId("turn-status")).toHaveText("Ready");
  await page.getByTestId("nav-issues").click();
  await page.getByRole("button", { name: "Open LIN-142" }).click();
  await expect(page.getByTestId("assignee-select")).toHaveValue("Maya Chen");
});

test("opens and highlights Maya Chen's issue from chat", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "open ticket for maya");
  await expect(page.getByTestId("current-view-title")).toHaveText("Issue Detail");
  await expect(page.getByTestId("selected-issue-id")).toHaveText("LIN-142");
  await expect(page.getByTestId("assignee-control")).toContainText("Maya Chen");

  await sendChat(page, "how do I assign Maya's ticket");
  await expect(page.getByTestId("current-view-title")).toHaveText("Issue Detail");
  await expect(page.getByTestId("assignee-control")).toHaveClass(/highlighted/);
  await expect(page.getByTestId("transcript")).toContainText(
    said("highlight the assignee control", "I'll open Issue Detail and highlight Assignee.")
  );
});

test("filters all tickets assigned to Maya instead of opening one ticket", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "all the tickets for Maya which are assigned to her");

  await expect(page.getByTestId("current-view-title")).toHaveText("Issues");
  await expect(page.getByTestId("issue-filter")).toContainText("Maya Chen");
  await expect(page.getByTestId("issue-count-badge")).toHaveText("1 open");
  await expect(page.getByTestId("transcript")).toContainText(
    said("I found 1 ticket assigned to Maya Chen: LIN-142", "I found 1 ticket for Maya Chen: LIN-142.")
  );
});

test("asks clarification for incomplete all-items requests", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "open all the");

  await expect(page.getByTestId("current-view-title")).toHaveText("Dashboard");
  await expect(page.getByTestId("transcript")).toContainText(
    definitionAuthority
      ? "Which records do you mean?"
      : "Do you mean all issues, all projects, or all tickets for a specific person?"
  );
});

test("asks targeted clarifying questions for vague create and assignment requests", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "create something new");
  await expect(page.getByTestId("current-view-title")).toHaveText("Dashboard");
  await expect(page.getByTestId("transcript")).toContainText(
    definitionAuthority
      ? "Which type of record would you like to create?"
      : "What should I create: a ticket, a project, a cycle, or a team member?"
  );

  await sendChat(page, "assign it");
  await expect(page.getByTestId("transcript")).toContainText(
    definitionAuthority
      ? "Who should this record be assigned to?"
      : "Who should I assign the current ticket to?"
  );
});

test("blocks broad workspace requests instead of showing other project data", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "show me all company projects");

  await expect(page.getByTestId("current-view-title")).toHaveText("Dashboard");
  await expect(page.getByTestId("transcript")).toContainText(
    definitionAuthority
      ? "I can only work within this workspace"
      : "I can only show work inside Product Engineering Workspace"
  );
});

test("uses previous issue context for person follow-ups", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "all tickets for Maya");
  await expect(page.getByTestId("issue-filter")).toContainText("Maya Chen");

  await sendChat(page, "what about Noah");

  await expect(page.getByTestId("current-view-title")).toHaveText("Issues");
  await expect(page.getByTestId("issue-filter")).toContainText("Noah Patel");
  await expect(page.getByTestId("issue-count-badge")).toHaveText("1 open");
});

test("shows an evaluator path and runs its first prompt", async ({ page }) => {
  await openApp(page);

  await expect(page.getByTestId("demo-path")).toContainText("Guided demo path");
  await expect(page.getByTestId("demo-path-1")).toContainText("Show sprint planning");

  await page.getByTestId("demo-path-1").click();
  await expect(page.getByTestId("turn-status")).not.toHaveText("Thinking");
  await expect(page.getByTestId("current-view-title")).toHaveText("Cycles");
});

test("answers capability and team-count questions conversationally", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "are you capable of doing");
  await expect(page.getByTestId("current-view-title")).toHaveText("Dashboard");
  await expect(page.getByTestId("transcript")).toContainText(
    said("I can guide this Pixel demo", "Here's what I can do in Pixel:")
  );

  await sendChat(page, "how many team members are there");
  await expect(page.getByTestId("current-view-title")).toHaveText("Teams");
  await expect(page.getByTestId("transcript")).toContainText(
    said("There are 2 team members", "Product Engineering Workspace has 2 team members.")
  );
});

test("understands misspellings, corrections, and current issue follow-ups", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "open tikit for maya");
  await expect(page.getByTestId("current-view-title")).toHaveText("Issue Detail");
  await expect(page.getByTestId("selected-issue-id")).toHaveText("LIN-142");

  await sendChat(page, "no not cycles show issues instead");
  await expect(page.getByTestId("current-view-title")).toHaveText("Issues");

  await page.getByLabel("Open LIN-137").click();
  await expect(page.getByTestId("selected-issue-id")).toHaveText("LIN-137");

  await sendChat(page, "how do I assign this issue");
  await expect(page.getByTestId("current-view-title")).toHaveText("Issue Detail");
  await expect(page.getByTestId("selected-issue-id")).toHaveText("LIN-137");
  await expect(page.getByTestId("assignee-control")).toHaveClass(/highlighted/);
});

test("updates the current issue from natural follow-up commands", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "open ticket for Maya");
  await expect(page.getByTestId("selected-issue-id")).toHaveText("LIN-142");

  await sendChat(page, "assign it to Noah");
  await expect(page.getByTestId("current-view-title")).toHaveText("Issue Detail");
  await expect(page.getByTestId("assignee-select")).toHaveValue("Noah Patel");
  await expect(page.getByText("Updated now")).toBeVisible();
  // The receipt is spoken only after the keyed change commits; nothing claims it earlier.
  await expect(page.getByTestId("transcript")).toContainText("Updated LIN-142: assignee to Noah Patel.");
  await expect(page.getByTestId("transcript")).not.toContainText("Done. I updated");

  await sendChat(page, "make it low priority");
  const prioritySelect = page.locator(".property-row").filter({ hasText: "Priority" }).locator("select");
  await expect(prioritySelect).toHaveValue("Low");

  await sendChat(page, "what did we just change?");
  await expect(page.getByTestId("transcript")).toContainText("The most recent change: updated LIN-142.");
});

test("creates a demo ticket and opens the new issue", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "open a fresh ticket for Maya");
  if (definitionAuthority) {
    await expect(page.getByTestId("transcript")).toContainText("What should the title of the new ticket be?");
    await sendChat(page, "Investigate customer onboarding issue");
    await expect(page.getByTestId("transcript")).toContainText("Which project should the new ticket have");
    await sendChat(page, "Issue Triage Workflow");
  }

  await expect(page.getByTestId("turn-status")).toHaveText("Ready");
  await expect(page.getByTestId("current-view-title")).toHaveText("Issue Detail");
  await expect(page.getByTestId("selected-issue-id")).toHaveText("PIX-143");
  await expect(page.getByTestId("assignee-control")).toContainText("Maya Chen");
  await expect(page.getByText("Created now")).toBeVisible();
  await expect(page.getByTestId("transcript")).toContainText("Created PIX-143:");
  await expect(page.getByTestId("transcript")).not.toContainText("I created");
  await expect(page.getByTestId("activity-popup")).toContainText(
    "Saved to Product Engineering Workspace: created PIX-143 for Maya Chen."
  );

  await page.getByTestId("nav-issues").click();
  await expect(page.getByText("PIX-143 - Maya Chen - Issue Triage")).toBeVisible();
});

test("keeps created demo records after a browser refresh", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "open a fresh ticket for Maya");
  if (definitionAuthority) {
    await expect(page.getByTestId("transcript")).toContainText("What should the title of the new ticket be?");
    await sendChat(page, "Investigate customer onboarding issue");
    await expect(page.getByTestId("transcript")).toContainText("Which project should the new ticket have");
    await sendChat(page, "Issue Triage Workflow");
  }
  await expect(page.getByTestId("selected-issue-id")).toHaveText("PIX-143");

  await page.reload();
  await expect(page.getByTestId("current-view-title")).toHaveText("Dashboard");
  await page.getByTestId("nav-issues").click();

  await expect(page.getByText("PIX-143 - Maya Chen - Issue Triage")).toBeVisible();
});

test("shows and highlights the create ticket entry point", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "where to create tickets for the users?");

  await expect(page.getByTestId("current-view-title")).toHaveText("Issues");
  await expect(page.getByTestId("create-ticket-button")).toHaveClass(/highlighted-action/);
  await expect(page.getByTestId("transcript")).toContainText("highlight Create ticket");
});

test("asks who should own a ticket before opening an incomplete create flow", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "create a ticket");

  await expect(page.getByTestId("current-view-title")).toHaveText("Issues");
  await expect(page.getByTestId("create-ticket-button")).toHaveClass(/highlighted-action/);
  await expect(page.getByTestId("issue-create-panel")).toBeHidden();
  await expect(page.getByTestId("transcript")).toContainText(
    said("Who should own this ticket?", "Who should own the new ticket?")
  );
});

test("validates an unknown assignee before continuing ticket creation", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "create a high priority ticket for Lucifer about GitHub onboarding");

  await expect(page.getByTestId("current-view-title")).toHaveText("Teams");
  await expect(page.getByTestId("team-member-create-panel")).toBeVisible();
  await expect(page.getByTestId("member-name-input")).toHaveValue("Lucifer");
  await expect(page.getByTestId("transcript")).toContainText(
    said("Lucifer is not in the team directory yet", "I can't find Lucifer in this workspace.")
  );
  await expect(page.getByTestId("transcript")).not.toContainText("I created");

  await page.getByTestId("submit-create-member").click();

  await expect(page.getByTestId("current-view-title")).toHaveText("Issues");
  await expect(page.getByTestId("issue-create-panel")).toBeVisible();
  await expect(page.getByTestId("ticket-assignee-select")).toHaveValue("Lucifer");
  await expect(page.getByTestId("ticket-title-input")).toHaveValue("Github Onboarding");
  await expect(page.getByTestId("transcript")).toContainText(
    "Lucifer has been added. I'll open the ticket form with Lucifer selected."
  );

  await page.getByTestId("submit-create-ticket").click();

  await expect(page.getByTestId("current-view-title")).toHaveText("Issue Detail");
  await expect(page.getByTestId("assignee-control")).toContainText("Lucifer");
  await expect(page.getByText("Created now")).toBeVisible();
});

test("uses voice transcripts as voice-mode turns through the same action pipeline", async ({
  page
}) => {
  await installMockVoice(page);
  let sawVoiceMode = false;

  await page.route(agentTurnRoute, async (route) => {
    const body = route.request().postDataJSON();
    if (body?.message === "show sprint planning") {
      sawVoiceMode = body.input_mode === "voice";
    }
    const response = await route.fetch({
      url: freshBackendUrl(route.request().url())
    });
    await route.fulfill({ response });
  });

  await openApp(page);
  await expect(page.getByTestId("voice-status")).toHaveText("Voice: Ready");

  await page.getByTestId("voice-toggle").click();
  await expect(page.getByTestId("voice-status")).toHaveText("Voice: Listening");

  await page.evaluate(() => window.__emitVoiceTranscript?.("show sprint planning"));

  await expect(page.getByTestId("current-view-title")).toHaveText("Cycles");
  await expect(page.getByTestId("transcript")).toContainText("show sprint planning");
  expect(sawVoiceMode).toBe(true);
});

test("waits for voice silence before sending the completed spoken turn", async ({ page }) => {
  await installMockVoice(page);
  const voiceMessages: string[] = [];

  await page.route(agentTurnRoute, async (route) => {
    const body = route.request().postDataJSON();
    voiceMessages.push(body.message);
    const response = await route.fetch({
      url: freshBackendUrl(route.request().url())
    });
    await route.fulfill({ response });
  });

  await openApp(page);

  await page.getByTestId("voice-toggle").click();
  await page.evaluate(() => window.__emitVoiceTranscript?.("show sprint"));
  await page.waitForTimeout(25);
  await page.evaluate(() => window.__emitVoiceTranscript?.("planning"));

  await expect(page.getByTestId("current-view-title")).toHaveText("Cycles");
  expect(voiceMessages).toEqual(["show sprint planning"]);
});

test("handles voice typo input and voice-only demo issue creation", async ({ page }) => {
  await installMockVoice(page);
  await openApp(page);

  await page.getByTestId("voice-toggle").click();
  await page.evaluate(() => window.__emitVoiceTranscript?.("open tikit for maya"));

  await expect(page.getByTestId("current-view-title")).toHaveText("Issue Detail");
  await expect(page.getByTestId("selected-issue-id")).toHaveText("LIN-142");

  await page.getByTestId("voice-toggle").click();
  await expect(page.getByTestId("voice-status")).toHaveText("Voice: Listening");
  await page.evaluate(() => window.__emitVoiceTranscript?.("open a fresh ticket for maya"));

  if (definitionAuthority) {
    await expect(page.getByTestId("transcript")).toContainText("What should the title of the new ticket be?");
    await page.evaluate(() => window.__emitVoiceTranscript?.("Investigate customer onboarding issue"));
    await expect(page.getByTestId("transcript")).toContainText("Which project should the new ticket have");
    await page.evaluate(() => window.__emitVoiceTranscript?.("Issue Triage Workflow"));
  }
  await expect(page.getByTestId("turn-status")).toHaveText("Ready");
  await expect(page.getByTestId("selected-issue-id")).toContainText(/^PIX-\d+$/);
  await expect(page.getByTestId("transcript")).toContainText("Created PIX-");
  await expect(page.getByTestId("current-view-title")).toHaveText("Issue Detail");
});

test("speaks assistant replies after voice input", async ({ page }) => {
  await installMockVoice(page);
  await openApp(page);

  await page.getByTestId("voice-toggle").click();
  await page.evaluate(() => window.__emitVoiceTranscript?.("show sprint planning"));

  await expect(page.getByTestId("current-view-title")).toHaveText("Cycles");
  await expect
    .poll(async () => page.evaluate(() => window.__spokenAgentReplies ?? []))
    .toContainEqual(expect.stringContaining(
      definitionAuthority ? "I'll open Cycles." : "I'll show you the current cycle"
    ));
});

test("answers identity questions without generic routing", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "Hii there who are u");

  await expect(page.getByTestId("transcript")).toContainText(
    said("I'm Edith, Pixel's live demo guide.", "I'm Edith, your guide to Pixel.")
  );
});

test("handles greetings, capabilities, and visitor introduction naturally", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "Hii There?");
  await expect(page.getByTestId("transcript")).toContainText(
    said("Hi there. What would you like to explore first in Pixel?", "Hi, I'm Edith, your guide to Pixel.")
  );

  await sendChat(page, "What are you capable of doing?");
  await expect(page.getByTestId("transcript")).toContainText(
    said("I can guide this Pixel demo through", "Here's what I can do in Pixel:")
  );

  await sendChat(page, "HI there i am Pushkar!");
  await expect(page.getByTestId("transcript")).toContainText(
    said("Nice to meet you, Pushkar.", "Nice to meet you, Pushkar.")
  );

  await sendChat(page, "Hi");
  await expect(page.getByTestId("transcript")).toContainText(
    said("Hi Pushkar. What would you like to explore next in Pixel?",
         "Hi Pushkar, good to see you again. What would you like to explore next in Pixel?")
  );
});

test("does not treat mixed create-and-assign phrasing as old ticket lookup", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "open a ticket for Maya and assign to Jen");

  await expect(page.getByTestId("current-view-title")).toHaveText("Teams");
  await expect(page.getByTestId("team-member-create-panel")).toBeVisible();
  await expect(page.getByTestId("member-name-input")).toHaveValue("Jen");
  await expect(page.getByTestId("transcript")).toContainText(
    said("Jen is not in the team directory yet", "I can't find Jen in this workspace.")
  );
});

test("opens add-member flow from natural member creation phrasing", async ({ page }) => {
  await openApp(page);

  await sendChat(page, "can you add a new member Lucife");

  await expect(page.getByTestId("current-view-title")).toHaveText("Teams");
  await expect(page.getByTestId("team-member-create-panel")).toBeVisible();
  await expect(page.getByTestId("member-name-input")).toHaveValue("Lucife");
});

test("turns agent speech into listening when the user presses voice", async ({ page }) => {
  await installMockVoice(page);
  await openApp(page);

  await page.getByTestId("voice-toggle").click();
  await page.evaluate(() => window.__emitVoiceTranscript?.("show sprint planning"));
  await expect(page.getByTestId("voice-status")).toHaveText("Voice: Speaking");

  await page.getByTestId("voice-toggle").click();

  await expect(page.getByTestId("voice-status")).toHaveText("Voice: Listening");
});

test("stops agent speech and listens when the visitor presses voice during speech", async ({ page }) => {
  await installMockVoice(page);
  const voiceMessages: string[] = [];

  await page.route(agentTurnRoute, async (route) => {
    const body = route.request().postDataJSON();
    voiceMessages.push(body.message);
    const response = await route.fetch({
      url: freshBackendUrl(route.request().url())
    });
    await route.fulfill({ response });
  });

  await openApp(page);

  await page.getByTestId("voice-toggle").click();
  await page.evaluate(() => window.__emitVoiceTranscript?.("show sprint planning"));
  await expect(page.getByTestId("voice-status")).toHaveText("Voice: Speaking");

  await page.getByTestId("voice-toggle").click();
  await expect(page.getByTestId("voice-status")).toHaveText("Voice: Listening");
  await page.evaluate(() => window.__emitVoiceTranscript?.("set up github integration"));

  await expect(page.getByTestId("current-view-title")).toHaveText("Integrations");
  await expect(page.getByTestId("github-setup-panel")).toContainText("Select repositories");
  expect(voiceMessages).toEqual(["show sprint planning", "set up github integration"]);
});

test("opens integrations for GitHub and Slack, then blocks out-of-product requests", async ({
  page
}) => {
  await openApp(page);

  await sendChat(page, "how does github integration work");
  await expect(page.getByTestId("current-view-title")).toHaveText("Integrations");
  await expect(page.getByTestId("github-integration-card")).toHaveClass(/highlighted-card/);

  await sendChat(page, "What can Pixel do with Slack?");
  await expect(page.getByTestId("current-view-title")).toHaveText("Integrations");
  await expect(page.getByTestId("slack-integration-card")).toHaveClass(/highlighted-card/);
  await expect(page.getByTestId("transcript")).toContainText(
    said("Slack lets teams create issues", "I'll open Integrations and highlight Slack.")
  );

  await sendChat(page, "set up github integration");
  await expect(page.getByTestId("current-view-title")).toHaveText("Integrations");
  await expect(page.getByTestId("github-integration-card")).toHaveClass(/highlighted-card/);
  await expect(page.getByTestId("github-setup-panel")).toContainText("Select repositories");
  await expect(page.getByTestId("transcript")).toContainText(
    said("GitHub setup flow", "I'll open Integrations and highlight GitHub setup.")
  );

  await sendChat(page, "open salesforce");
  await expect(page.getByTestId("turn-status")).toHaveText("Action blocked");
  await expect(page.getByTestId("current-view-title")).toHaveText("Integrations");
  await expect(page.getByTestId("transcript")).toContainText(
    definitionAuthority
      ? "I can only help with Pixel here"
      : "I can only demonstrate Pixel workflows here"
  );
});

test("cancels an active turn and ignores the delayed stale result", async ({ page }) => {
  let turnRequestCount = 0;
  let releaseFirstTurn: () => void = () => undefined;
  const firstTurnCanContinue = new Promise<void>((resolve) => {
    releaseFirstTurn = resolve;
  });

  await page.route(agentCancelTurnOneRoute, async (route) => {
    await route.fulfill({
      contentType: "application/json",
      json: {
        session_id: "cancelled",
        turn_id: 1,
        status: "cancelled"
      }
    });
  });

  await page.route(agentTurnRoute, async (route) => {
    turnRequestCount += 1;
    if (turnRequestCount === 1) {
      await firstTurnCanContinue;
      await route.fulfill({
        contentType: "application/json",
        json: {
          session_id: "delayed",
          turn_id: 1,
          status: "completed",
          speech: "I'll show you the current cycle.",
          proposed_action: { type: "OPEN_CYCLES", payload: {} },
          validated_action: { type: "OPEN_CYCLES", payload: {} },
          intent_trace: {
            goal: "Sprint planning",
            relevant_feature: "Cycles",
            reason: "Delayed response used by the stale-turn test.",
            confidence: 0.8,
            status: "active"
          },
          signals: [],
          retrieved_context: []
        }
      });
      return;
    }
    const response = await route.fetch({
      url: freshBackendUrl(route.request().url())
    });
    await route.fulfill({ response });
  });

  await openApp(page);

  await page.getByTestId("chat-input").fill("show sprint planning");
  await page.getByTestId("chat-send").click();
  await expect(page.getByTestId("turn-status")).toHaveText("Thinking");

  try {
    const cancelRequest = page.waitForRequest((request) =>
      new URL(request.url()).pathname === "/api/agent/turn/1/cancel"
    );
    await page.getByTestId("chat-input").fill("open ticket for maya");
    await page.getByTestId("chat-send").click();
    await cancelRequest;

    await expect(page.getByTestId("current-view-title")).toHaveText("Issue Detail");
    await expect(page.getByTestId("selected-issue-id")).toHaveText("LIN-142");
  } finally {
    releaseFirstTurn();
  }


  await expect(page.getByTestId("current-view-title")).toHaveText("Issue Detail");
  await expect(page.getByTestId("selected-issue-id")).toHaveText("LIN-142");
});

test("blocks further demo actions when the agent API disconnects", async ({ page }) => {
  await page.route(agentTurnRoute, (route) => route.abort());
  await openApp(page);

  await sendChat(page, "show sprint planning");

  await expect(page.getByTestId("turn-status")).toHaveText("Service unavailable");
  await expect(page.getByTestId("service-readiness")).toContainText("disconnected");
  await expect(page.getByTestId("chat-input")).toBeDisabled();
  await expect(page.getByTestId("transcript")).not.toContainText("backend is running");
});
