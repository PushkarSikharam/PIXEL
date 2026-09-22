import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test, type Page } from "@playwright/test";
import { openApp, sendChat, setupIsolatedApp } from "../../../tests/e2e/harness";

// Records what the web app does for each golden conversation. Before 5c the browser answered some
// turns on its own; 5c moves every decision to the backend, and this recording is how that move is
// held to the demo's behaviour.
//
// The recording is evidence, never a target: it is not regenerated to make a test pass. Every field
// that differs from it is listed, with its kind and reason, in golden/browser_differences.json, and
// a listed difference that no longer occurs also fails (as golden_parity.py does for the backend).
//
// Capture what the app does now, for review, without touching the recording:
//   PIXEL_CAPTURE_GOLDEN=<file> npx playwright test products/linear_simplified/tests/browser-golden.spec.ts
// Regenerate the recording (only with a justified behaviour change): PIXEL_UPDATE_GOLDEN=1 npm run test:e2e

type GoldenCase = { id: string; workspace?: string; turns: string[] };
type TurnSnapshot = {
  message: string;
  handled_by: "browser" | "backend";
  path: string;
  view: string | null;
  reply: string | null;
  turn_status: string | null;
  highlighted: string[];
  issue_filter: string | null;
  open_issue: string | null;
};

type Difference = { case: string; turn: number; field: string; recorded: unknown; current: unknown };
type ReviewedDifference = Difference & { kind: "wording" | "behaviour" | "security"; reason: string };

const GOLDEN_DIR = join(__dirname, "golden");
const RECORDING = join(GOLDEN_DIR, "browser_decisions.json");
const DIFFERENCES = join(GOLDEN_DIR, "browser_differences.json");
const UPDATING = process.env.PIXEL_UPDATE_GOLDEN === "1";
const CAPTURE = process.env.PIXEL_CAPTURE_GOLDEN;
const DEFINITION_AUTHORITY = process.env.PIXEL_ENGINE_MODE === "definition";
const cases: GoldenCase[] = JSON.parse(readFileSync(join(GOLDEN_DIR, "conversations.json"), "utf-8")).cases;
const recorded: Record<string, TurnSnapshot[]> = UPDATING ? {} : JSON.parse(readFileSync(RECORDING, "utf-8"));
const reviewed: ReviewedDifference[] = UPDATING
  ? []
  : loadReviewed().filter((entry) =>
    DEFINITION_AUTHORITY
    || (entry.field === "handled_by" && entry.recorded === "browser" && entry.current === "backend")
    || (entry.field === "reply" && entry.reason.includes("execution keys"))
    || entry.kind === "security"
  );
const captured: Record<string, TurnSnapshot[]> = {};

setupIsolatedApp();

test.describe.configure({ mode: "serial" });

test.afterAll(() => {
  const ordered = Object.fromEntries(cases.map(({ id }) => [id, captured[id]]));
  if (UPDATING) writeFileSync(RECORDING, `${JSON.stringify(ordered, null, 2)}\n`);
  if (CAPTURE) writeFileSync(CAPTURE, `${JSON.stringify(ordered, null, 2)}\n`);
});

test("golden recording covers every conversation", () => {
  test.skip(UPDATING, "Recording in progress.");
  expect(Object.keys(recorded).sort()).toEqual(cases.map(({ id }) => id).sort());
});

for (const goldenCase of cases) {
  test(`golden browser decisions: ${goldenCase.id}`, async ({ page }) => {
    await openApp(page);
    if (goldenCase.workspace) {
      await page.getByTestId("workspace-switcher").selectOption(goldenCase.workspace);
    }

    const snapshots: TurnSnapshot[] = [];
    for (const message of goldenCase.turns) {
      snapshots.push(await runTurn(page, message));
      if (snapshots.at(-1)?.path !== "/") break;
    }
    captured[goldenCase.id] = snapshots;

    if (!UPDATING && !CAPTURE) {
      const found = differences(goldenCase.id, recorded[goldenCase.id], snapshots);
      const listed = reviewed.filter((entry) => entry.case === goldenCase.id);
      const key = (d: Difference) => `${d.turn}.${d.field}`;
      const listedByKey = new Map(listed.map((entry) => [key(entry), entry]));
      const foundByKey = new Map(found.map((difference) => [key(difference), difference]));
      const unlisted = found.filter((d) =>
        !listedByKey.has(key(d)) && !legacyReviewedSecurity(d) && !legacyReviewedWording(d)
      );
      const stale = DEFINITION_AUTHORITY
        ? listed.filter((entry) => !foundByKey.has(key(entry))).map(key)
        : [];
      const mismatched = DEFINITION_AUTHORITY
        ? listed.filter((entry) => {
          const actual = foundByKey.get(key(entry));
          return actual !== undefined
            && (!same(actual.recorded, entry.recorded) || !same(actual.current, entry.current));
        }).map(key)
        : [];
      expect({ unlisted, stale, mismatched }).toEqual({ unlisted: [], stale: [], mismatched: [] });
    }
  });
}

function legacyReviewedSecurity(entry: Difference): boolean {
  return !DEFINITION_AUTHORITY
    && entry.case === "update-outside-person"
    && entry.turn === 1
    && ["highlighted", "open_issue", "reply", "view"].includes(entry.field);
}

function legacyReviewedWording(entry: Difference): boolean {
  return !DEFINITION_AUTHORITY && entry.field === "reply";
}

function differences(caseId: string, before: TurnSnapshot[], after: TurnSnapshot[]): Difference[] {
  const found: Difference[] = [];
  if (before.length !== after.length) {
    found.push({ case: caseId, turn: 0, field: "__turns__", recorded: before.length, current: after.length });
  }
  before.slice(0, after.length).forEach((old, turn) => {
    const current = after[turn] as Record<string, unknown>;
    const recordedTurn = old as Record<string, unknown>;
    for (const field of [...new Set([...Object.keys(recordedTurn), ...Object.keys(current)])].sort()) {
      if (!same(recordedTurn[field], current[field])) {
        found.push({ case: caseId, turn, field, recorded: recordedTurn[field], current: current[field] });
      }
    }
  });
  return found;
}

function same(left: unknown, right: unknown): boolean {
  return JSON.stringify(left ?? null) === JSON.stringify(right ?? null);
}

function loadReviewed(): ReviewedDifference[] {
  const entries: ReviewedDifference[] = JSON.parse(readFileSync(DIFFERENCES, "utf-8"));
  const keys = ["case", "current", "field", "kind", "reason", "recorded", "turn"];
  const seen = new Set<string>();
  entries.forEach((entry, index) => {
    if (JSON.stringify(Object.keys(entry).sort()) !== JSON.stringify(keys)) {
      throw new Error(`browser difference ${index} must have exactly the keys ${keys.join(", ")}`);
    }
    if (!["wording", "behaviour", "security"].includes(entry.kind)) {
      throw new Error(`browser difference ${index} has unknown kind ${entry.kind}`);
    }
    if (entry.reason.trim().length < 20) throw new Error(`browser difference ${index} needs a real reason`);
    const location = `${entry.case}[${entry.turn}].${entry.field}`;
    if (seen.has(location)) throw new Error(`browser difference ${index} repeats ${location}`);
    seen.add(location);
  });
  return entries;
}

const agentReplies = (page: Page) => page.locator('[data-testid="transcript"] article.message:not(.visitor-message) p');

async function runTurn(page: Page, message: string): Promise<TurnSnapshot> {
  let backendTurns = 0;
  const countTurns = (request: { url(): string }) => {
    if (new URL(request.url()).pathname === "/api/agent/turn") backendTurns += 1;
  };
  page.on("request", countTurns);
  const repliesBefore = await agentReplies(page).count();

  await sendChat(page, message);
  await expect
    .poll(async () => new URL(page.url()).pathname !== "/" || (await agentReplies(page).count()) > repliesBefore)
    .toBe(true);
  await page.waitForLoadState("networkidle");
  page.off("request", countTurns);

  const path = new URL(page.url()).pathname;
  const onApp = path === "/";
  return {
    message,
    handled_by: backendTurns > 0 ? "backend" : "browser",
    path,
    view: onApp ? await textOf(page, '[data-testid="current-view-title"]') : null,
    reply: onApp ? (await agentReplies(page).last().textContent())?.trim() ?? null : null,
    turn_status: onApp ? await textOf(page, '[data-testid="turn-status"]') : null,
    highlighted: onApp ? await highlightedTargets(page) : [],
    issue_filter: onApp ? await textOf(page, '[data-testid="issue-filter"]') : null,
    open_issue: onApp ? await firstLineOf(page, '[data-testid="issue-detail-panel"]') : null
  };
}

async function textOf(page: Page, selector: string): Promise<string | null> {
  const element = page.locator(selector).first();
  if (!(await element.isVisible().catch(() => false))) return null;
  return ((await element.textContent()) ?? "").replace(/\s+/g, " ").trim();
}

async function firstLineOf(page: Page, selector: string): Promise<string | null> {
  const element = page.locator(selector).first();
  if (!(await element.isVisible().catch(() => false))) return null;
  return (await element.innerText()).split("\n").map((line) => line.trim()).find(Boolean) ?? null;
}

async function highlightedTargets(page: Page): Promise<string[]> {
  return page.evaluate(() =>
    Array.from(document.querySelectorAll(".highlighted-action, .highlighted-card, .highlighted, .highlighted-bar"))
      .map((element) => element.closest("[data-testid]")?.getAttribute("data-testid") ?? element.className)
      .sort()
  );
}
