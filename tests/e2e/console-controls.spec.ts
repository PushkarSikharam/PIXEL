import { expect, test, type Page } from "@playwright/test";

/**
 * Every control a signed-in person can see does something.
 *
 * Each page of the console is opened against a stand-in for Pixel's server, and every visible,
 * enabled button and link on it is pressed, one at a time on a freshly loaded page. A press must
 * change something a person could notice - the address, the page, an open dialog or menu - or
 * ask the server for something. A control that does none of these is a dead button, and the
 * page must not throw or log an error while any of them is pressed. Menus are opened and each of
 * their items is pressed the same way.
 */

const SESSION = {
  user_id: "owner", email: "owner@example.test", tenant_id: "private", organization_name: "Northwind",
  role: "org_admin", team_id: null, console_product_id: null,
  teams: [{ team_id: "default", name: "My team" }, { team_id: "mobile", name: "Mobile" }],
};

const SHAPE = {
  product_id: "board", product_name: "Board", assistant_name: "Edith", definition_id: "board", definition_version: 1,
  views: [
    { name: "tickets", label: "Tickets", kind: "list", entity: "ticket", navigable: true, columns: ["title", "status"], controls: [] },
    { name: "people", label: "Team members", kind: "list", entity: "person", navigable: true, columns: ["name"], controls: [] },
  ],
  entities: [
    { name: "ticket", label: "Ticket", plural: "Tickets", title_field: "title", summary_fields: ["status"], is_people: false,
      fields: [
        { name: "title", label: "Title", type: "text", display: true, required: true, editable: true, values: [] },
        { name: "status", label: "Status", type: "enum", display: true, required: false, editable: true, values: ["Todo", "Done"] },
      ] },
    { name: "person", label: "Team member", plural: "Team members", title_field: "name", summary_fields: [], is_people: true,
      fields: [{ name: "name", label: "Name", type: "text", display: true, required: true, editable: true, values: [] }] },
  ],
  actions: [{ name: "create_ticket", client_type: "CREATE_TICKET", capability: "CREATE_RECORD", entity: "ticket", description: "Create a ticket", fields: ["title", "status"] }],
};

const RECORDS = {
  product_id: "board", scope: "primary",
  records: {
    ticket: [{ id: "T-1", title: "Fix login", status: "Todo", revision: 1 }],
    person: [{ id: "maya", name: "Maya Chen", title: "Maya Chen", revision: 1 }],
  },
};

type Seen = { errors: string[]; requests: number; unexpected: string[] };

async function standIn(page: Page, seen: Seen) {
  await page.addInitScript(() => sessionStorage.setItem("pixel.console.session",
    JSON.stringify({ token: "test-token", userId: "owner", tenantId: "private" })));
  await page.route("**/api/agent/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace(/^.*\/api\/agent/, "");
    const method = request.method();
    seen.requests += 1;
    if (path === "/account/session") return route.fulfill({ json: SESSION });
    if (path === "/account/logout") return route.fulfill({ status: 204, body: "" });
    if (path === "/organizations/private/products" && method === "GET") return route.fulfill({ json: { products: [{
      product_id: "board", name: "Board", team_id: "default", definition_id: "board", definition_version: 1, state: "active",
      visitor_access: false, entities: ["ticket", "person"], views: ["tickets", "people"],
    }] } });
    if (path === "/organizations/private/members") return route.fulfill({ json: { tenant_id: "private", members: [
      { user_id: "owner", email: "owner@example.test", role: "org_admin", team_id: null, team_name: null },
      { user_id: "sam", email: "sam@example.test", role: "team_member", team_id: "default", team_name: "My team" },
    ] } });
    if (path === "/organizations/private/teams" && method === "GET") return route.fulfill({ json: { tenant_id: "private", teams: [
      { team_id: "default", name: "My team", people: 1, products: ["board"] },
      { team_id: "mobile", name: "Mobile", people: 0, products: [] },
    ] } });
    if (path.startsWith("/organizations/private")) return route.fulfill({ json: { ok: true, name: "Northwind", team_id: "x", products: [], people: 0 } });
    if (path === "/products/board/shape") return route.fulfill({ json: SHAPE });
    if (path === "/products/board/records" && method === "GET") return route.fulfill({ json: RECORDS });
    if (path.startsWith("/products/board/records")) return route.fulfill({ json: { id: "T-2", title: "New", revision: 1 } });
    if (path === "/products/board/knowledge") return route.fulfill({ json: { version: 1, documents: [] } });
    if (path === "/turn") {
      const body = request.postDataJSON();
      return route.fulfill({ json: { session_id: body.session_id, turn_id: body.turn_id, status: "completed",
        speech: "Here is what I can do.", validated_action: null, execution: null } });
    }
    if (path.endsWith("/close")) return route.fulfill({ status: 204, body: "" });
    if (path === "/health") return route.fulfill({ json: { status: "ok", authority: "definition" } });
    if (path === "/speech") return route.fulfill({ status: 503, json: { detail: "Unavailable" } });
    // Links out of the console (the guided demo, the architecture page) load pages with their
    // own requests; only the console's are this test's business.
    if (new URL(page.url()).pathname.startsWith("/console")) seen.unexpected.push(`${method} ${path}`);
    return route.fulfill({ status: 404, json: { detail: "Unexpected test request" } });
  });
  page.on("pageerror", (error) => seen.errors.push(`page error: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error" && !/Failed to load resource/.test(message.text())) {
      seen.errors.push(`console: ${message.text()}`);
    }
  });
}

// Controls a person can reach: buttons and links, visible and not disabled. In-page anchors only
// move focus, and a control marked current (the page or screen already showing) is where the
// person already is, so neither is counted.
const CONTROLS = "button:not([disabled]):not([aria-current]):not([aria-pressed='true']), "
  + "a[href]:not([href^='#']):not([aria-current])";

async function controls(page: Page): Promise<Array<{ name: string; menu: boolean }>> {
  return page.locator(CONTROLS).evaluateAll((elements) => elements.map((element) => {
    const box = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    const visible = box.width > 0 && box.height > 0 && style.visibility !== "hidden" && style.display !== "none";
    const name = element.getAttribute("aria-label") || element.textContent?.trim() || element.getAttribute("title") || "(unnamed)";
    return { name: visible ? name.replace(/\s+/g, " ").slice(0, 60) : "",
             menu: element.getAttribute("aria-haspopup") === "menu" };
  }));
}

async function snapshot(page: Page) {
  return page.evaluate(() => ({
    url: location.pathname + location.search,
    text: document.body.innerText,
    dialogs: document.querySelectorAll("[role=dialog], [role=menu], [role=alertdialog]").length,
    expanded: [...document.querySelectorAll("[aria-expanded]")].map((e) => e.getAttribute("aria-expanded")).join(),
    focus: document.activeElement?.outerHTML.slice(0, 120) ?? "",
  }));
}

async function openPage(page: Page, path: string) {
  await page.goto(path, { waitUntil: "networkidle" });
  await expect(page.locator("main")).toBeVisible();
}

/** Press one control on a fresh page and say whether anything happened. */
async function press(page: Page, path: string, seen: Seen, index: number) {
  await openPage(page, path);
  const target = page.locator(CONTROLS).nth(index);
  const start = await snapshot(page);
  const requests = seen.requests;
  // A link that leaves the console is followed by the browser; the new address is the effect.
  await target.click({ timeout: 5_000 });
  // Give a navigation or a reply up to three seconds to show.
  for (let waited = 0; waited < 3_000; waited += 250) {
    await page.waitForTimeout(250);
    const end = await snapshot(page).catch(() => null);
    if (end === null) return true; // the page navigated away mid-read: that is an effect
    if (end.url !== start.url || end.text !== start.text || end.dialogs !== start.dialogs
        || end.expanded !== start.expanded || seen.requests !== requests) return true;
  }
  return false;
}

const PAGES = [
  "/console",
  "/console/products",
  "/console/products/board",
  "/console/products/new",
  "/console/organization",
  "/console/settings",
];

for (const path of PAGES) {
  test(`every visible control on ${path} does something`, async ({ page }) => {
    // The server is stood in for here, so which engine answers makes no difference; the second
    // browser pass, under definition authority, would only repeat this.
    test.skip(process.env.PIXEL_ENGINE_MODE === "definition", "Engine-independent; covered by the first pass.");
    test.setTimeout(240_000);
    await page.setViewportSize({ width: 1440, height: 1000 });
    const seen: Seen = { errors: [], requests: 0, unexpected: [] };
    await standIn(page, seen);
    await openPage(page, path);
    const found = await controls(page);
    const dead: string[] = [];
    let pressed = 0;
    for (const [index, { name, menu }] of found.entries()) {
      if (!name) continue;
      pressed += 1;
      if (!(await press(page, path, seen, index))) dead.push(name);
      // A menu's items only exist once it is open; each is pressed from a fresh, opened menu.
      if (menu) {
        await openPage(page, path);
        await page.locator(CONTROLS).nth(index).click();
        const items = await page.getByRole("menuitem").count();
        for (let item = 0; item < items; item += 1) {
          await openPage(page, path);
          await page.locator(CONTROLS).nth(index).click();
          const entry = page.getByRole("menuitem").nth(item);
          const label = (await entry.textContent())?.trim() || `item ${item}`;
          if (await entry.isDisabled()) continue;
          const start = await snapshot(page);
          const requests = seen.requests;
          await entry.click();
          await page.waitForTimeout(400);
          const end = await snapshot(page).catch(() => null);
          if (end && end.url === start.url && end.text === start.text && end.dialogs === start.dialogs
              && seen.requests === requests) dead.push(`${name} > ${label}`);
        }
      }
    }
    expect(pressed, `no controls were found on ${path}`).toBeGreaterThan(3);
    expect({ dead, errors: seen.errors, unexpected: seen.unexpected }).toEqual({ dead: [], errors: [], unexpected: [] });
  });
}
