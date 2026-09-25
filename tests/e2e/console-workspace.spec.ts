import { expect, test } from "@playwright/test";

// Leaving a product closes its conversation, and that request can still be in flight when a test
// ends. Let it finish while this file's routes answer it; otherwise it reaches the real backend
// port and is reported as an escaped request by whichever isolated test runs next.
test.afterEach(async ({ context }) => {
  for (const open of context.pages()) {
    await open.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => undefined);
  }
});

test("email sign-in never uses a shared demo identity", async ({ page }) => {
  let delivered = false;
  const requests: string[] = [];
  await page.route("**/api/agent/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    requests.push(path);
    if (path.endsWith("/account/email-code")) {
      expect(route.request().postDataJSON()).toEqual({ email: "owner@example.test" });
      delivered = true;
      return route.fulfill({ json: { challenge_id: "a".repeat(48), expires_in: 600 } });
    }
    if (path.endsWith("/account/verify-code")) {
      expect(delivered).toBe(true);
      expect(route.request().postDataJSON().code).toBe("12345678");
      return route.fulfill({ json: { token: "test-token", user_id: "owner", tenant_id: "private" } });
    }
    if (path.endsWith("/account/session")) return route.fulfill({ json: {
      user_id: "owner", email: "owner@example.test", tenant_id: "private", organization_name: "Private workspace",
      role: "org_admin", team_id: null, teams: [{ team_id: "default", name: "My team" }],
    } });
    if (path.endsWith("/products")) return route.fulfill({ json: { products: [] } });
    return route.fulfill({ status: 404, json: { detail: "Unexpected test request" } });
  });
  await page.goto("/sign-in");
  // "Your email", not "Work email": with sign-up open, the first code someone enters makes them
  // a workspace, and it need not be a company address.
  await page.getByLabel("Your email").fill("owner@example.test");
  await page.getByRole("button", { name: "Email me a code" }).click();
  await page.getByLabel("One-time code").fill("12345678");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Welcome to Pixel", exact: true })).toBeVisible();
  await expect(page.locator(".px-topbar")).toContainText("Private workspace");
  expect(requests.some((path) => path.includes("demo-login"))).toBe(false);
  await expect(page.getByRole("heading", { name: "No products yet", exact: true })).toBeVisible();
});

for (const mobile of [false, true]) {
  test(`product assistant is scoped and on the left (${mobile ? "mobile" : "desktop"})`, async ({ page }) => {
    await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 960 });
    await page.addInitScript(() => sessionStorage.setItem("pixel.console.session", JSON.stringify({ token: "test-token", userId: "owner", tenantId: "private" })));
    await page.addInitScript(() => {
      Object.defineProperty(window, "SpeechRecognition", { value: class {
        onresult: ((event: { results: Array<Array<{ transcript: string }>> }) => void) | null = null;
        onend: (() => void) | null = null;
        start() { this.onresult?.({ results: [[{ transcript: "A spoken question" }]] }); setTimeout(() => this.onend?.(), 0); }
        abort() { this.onend?.(); }
      } });
    });
    const turns: Array<{ product_id: string; session_id: string; message: string }> = [];
    let speechCalls = 0;
    await page.route("**/api/agent/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/account/session")) return route.fulfill({ json: {
        user_id: "owner", email: "owner@example.test", tenant_id: "private", organization_name: "Private workspace",
        role: "org_admin", team_id: null, teams: [{ team_id: "default", name: "My team" }],
      } });
      if (path.endsWith("/organizations/private/products")) return route.fulfill({ json: { products: ["alpha", "beta"].map((id) => ({
        product_id: id, name: id.toUpperCase(), definition_id: id, definition_version: 1, state: "active", visitor_access: false, entities: ["entry"], views: ["entries"],
      })) } });
      const product = path.includes("/beta/") ? "beta" : "alpha";
      if (path.endsWith("/shape")) return route.fulfill({ json: {
        product_id: product, product_name: product.toUpperCase(), assistant_name: "Edith", definition_id: product, definition_version: 1,
        views: [{ name: "entries", label: "Entries", kind: "list", entity: "entry", navigable: true, columns: ["title"], controls: [] }],
        entities: [{ name: "entry", label: "Entry", plural: "Entries", title_field: "title", summary_fields: ["title"], fields: [{ name: "title", label: "Title", type: "text", display: true }] }],
        actions: [{ name: "open_entry", client_type: "OPEN_ENTRY", capability: "OPEN_RECORD", entity: "entry", description: "Open entry", fields: [] }],
      } });
      if (path.endsWith("/records")) return route.fulfill({ json: { product_id: product, scope: "primary", records: { entry: [{ id: "E-1", title: `${product} record` }] } } });
      if (path.endsWith("/knowledge")) return route.fulfill({ json: { version: 1, documents: [] } });
      if (path.endsWith("/turn")) {
        const body = route.request().postDataJSON();
        turns.push(body);
        const validated = body.message === "Open entry"
          ? { type: "OPEN_ENTRY", payload: { record_id: "E-1" } }
          : body.message === "Leave product"
            ? { type: "OPEN_PRODUCTS", payload: { view: "products" } }
            : null;
        return route.fulfill({ json: { session_id: body.session_id, turn_id: body.turn_id, status: "completed", speech: body.message === "Open entry" ? "Opening the entry." : `Only ${body.product_id} content.`,
          validated_action: validated, execution: null } });
      }
      if (path.endsWith("/speech")) { speechCalls++; return route.fulfill({ status: 503, json: { detail: "Unavailable" } }); }
      return route.fulfill({ status: 404, json: { detail: "Unexpected test request" } });
    });
    // Enter through the product list. The product link must open its generated workspace before
    // any assistant or isolation behavior can count as a customer-facing capability.
    await page.goto("/console/products");
    await page.getByRole("link", { name: "ALPHA" }).click();
    await expect(page).toHaveURL(/\/console\/products\/alpha$/);
    if (mobile) {
      const navigation = page.getByRole("navigation", { name: "Primary" });
      await expect(navigation).not.toBeVisible();
      await page.getByRole("button", { name: "Open navigation" }).click();
      await expect(navigation).toBeVisible();
      await page.getByRole("button", { name: "Close navigation" }).click();
      await expect(navigation).not.toBeVisible();
    }
    const assistant = page.getByRole("complementary", { name: /^Edith, answering for / });
    await expect(assistant).toBeVisible();
    if (mobile) {
      // Folded on a phone: the page is what somebody came for, and the assistant is a tap away.
      await expect(assistant.getByLabel("Ask Edith")).toHaveCount(0);
      await assistant.getByRole("button", { name: "Show Edith" }).click();
    }
    await expect(assistant.getByLabel("Ask Edith")).toBeVisible();
    expect(speechCalls).toBe(0);
    await assistant.getByLabel("Ask Edith").fill("Tell me about alpha");
    await assistant.getByRole("button", { name: "Send message" }).click();
    await expect(assistant.getByText("Only alpha content.")).toBeVisible();
    if (!mobile) {
      const chatBox = await assistant.boundingBox();
      const tableBox = await page.getByRole("list", { name: "Entries" }).boundingBox();
      expect(chatBox!.x + chatBox!.width).toBeLessThanOrEqual(tableBox!.x);
    }
    await page.screenshot({ path: `test-results/product-assistant-${mobile ? "mobile" : "desktop"}.png`, fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await assistant.getByLabel("Ask Edith").fill("Open entry");
    await assistant.getByRole("button", { name: "Send message" }).click();
    await expect(page.getByRole("region", { name: "Selected record" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "alpha record" })).toBeVisible();
    await assistant.getByRole("button", { name: "Hide Edith" }).click();
    await expect(assistant.getByLabel("Ask Edith")).toHaveCount(0);
    await assistant.getByRole("button", { name: "Show Edith" }).click();
    await page.goto("/console/products/beta");
    await expect(assistant).toBeVisible();
    if (mobile) await assistant.getByRole("button", { name: "Show Edith" }).click();
    await expect(assistant.getByText("Only alpha content.")).toHaveCount(0);
    await assistant.getByLabel("Ask Edith").fill("Tell me about beta");
    await assistant.getByRole("button", { name: "Send message" }).click();
    await expect(assistant.getByText("Only beta content.")).toBeVisible();
    expect(turns.map((turn) => turn.product_id)).toEqual(["alpha", "alpha", "beta"]);
    expect(turns[0].session_id).toBe(turns[1].session_id);
    expect(turns[0].session_id).not.toBe(turns[2].session_id);
    expect(speechCalls).toBe(0);
    await assistant.getByRole("button", { name: "Start Voice input" }).click();
    await expect(assistant.getByText("A spoken question", { exact: true })).toBeVisible();
    await expect(assistant.getByText("Voice is unavailable. Text remains available.").first()).toBeVisible();
    expect(speechCalls).toBe(1);
    await assistant.getByLabel("Ask Edith").fill("Leave product");
    await assistant.getByRole("button", { name: "Send message" }).click();
    await expect(page).toHaveURL(/\/console\/products$/);
  });
}
