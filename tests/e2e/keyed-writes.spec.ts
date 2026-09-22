import { expect, test, type Request } from "@playwright/test";
import { openApp, sendChat, setupIsolatedApp } from "./harness";

// Milestone 3.2 slice 5c browser proof: production `app.main` runs in definition mode, the new
// engine answers turns with execution keys, the browser writes an assistant change only under its
// key and with exactly the bound change, and it shows only the backend's receipt.
setupIsolatedApp({ entry: "new-engine" });

function issueWrites(page: import("@playwright/test").Page): Request[] {
  const writes: Request[] = [];
  page.on("request", (request) => {
    if (/\/api\/agent\/demo-data\/issues(\/|$)/.test(new URL(request.url()).pathname)
      && request.method() !== "GET") {
      writes.push(request);
    }
  });
  return writes;
}

test("an assistant change is written only under its key and shows the backend receipt", async ({ page }) => {
  const writes = issueWrites(page);
  await openApp(page);
  await sendChat(page, "Open Maya's ticket");
  const keyed = page.waitForResponse((response) =>
    response.request().method() === "PATCH" && response.url().includes("/demo-data/issues/LIN-142")
  );
  await sendChat(page, "assign LIN-142 to Noah");
  expect((await keyed).status()).toBe(200);
  await expect(page.getByTestId("transcript")).toContainText("Updated LIN-142: assignee to Noah Patel.");

  expect(writes).toHaveLength(1);
  const request = writes[0];
  const headers = request.headers();
  expect(request.method()).toBe("PATCH");
  expect(headers["x-execution-key"]).toBeTruthy();
  expect(headers["x-session-id"]).toBeTruthy();
  expect(headers["idempotency-key"]).toBeUndefined();
  expect(request.postDataJSON()).toEqual({ changes: { assignee: "Noah Patel" } });

  await sendChat(page, "What changed?");
  await expect(page.getByTestId("transcript")).toContainText("The most recent change: updated LIN-142.");
});

test("a refused receipt is shown and nothing claims the change was made", async ({ page }) => {
  await openApp(page);
  await sendChat(page, "Open Maya's ticket");
  await page.route("**/api/agent/demo-data/issues/LIN-142", async (route) => {
    if (route.request().method() !== "PATCH") return route.fallback();
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        outcome: "refused",
        code: "superseded",
        speech: "A newer request replaced this change, so it wasn't applied.",
        record: null
      })
    });
  });
  await sendChat(page, "assign LIN-142 to Noah");
  const transcript = page.getByTestId("transcript");
  await expect(transcript).toContainText("A newer request replaced this change, so it wasn't applied.");
  await expect(transcript).not.toContainText("Updated LIN-142");
});

test("a form write still carries no execution key", async ({ page }) => {
  const writes = issueWrites(page);
  await openApp(page);
  await page.getByTestId("nav-issues").click();
  await page.getByTestId("create-ticket-button").click();
  await page.getByTestId("ticket-title-input").fill("Form ticket without a key");
  await page.getByTestId("ticket-assignee-select").selectOption("Noah Patel");
  const saved = page.waitForResponse((response) =>
    response.url().endsWith("/demo-data/issues") && response.request().method() === "POST"
  );
  await page.getByTestId("submit-create-ticket").click();
  expect((await saved).ok()).toBe(true);
  expect(writes).toHaveLength(1);
  expect(writes[0].headers()["x-execution-key"]).toBeUndefined();
  expect(writes[0].headers()["x-session-id"]).toBeUndefined();
});
