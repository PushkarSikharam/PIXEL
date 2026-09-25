import { spawn, type ChildProcess } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createServer } from "node:net";
import { expect, test, type Page, type Route } from "@playwright/test";
import { venvPython } from "../../scripts/venv-python.mjs";
import { E2E_SENTINEL_LOG } from "./ports";

// Shared isolation harness for browser tests: a fresh API process with its own database,
// paid providers switched off, external requests blocked, and a sentinel that records any
// request escaping to the Next.js rewrite target.

export let apiPort: number;
export const agentApiRoute = "**/api/agent/**";
export const agentTurnRoute = "**/api/agent/turn";
export const agentCancelTurnOneRoute = "**/api/agent/turn/1/cancel";

declare global {
  interface Window {
    __demoVoiceSilenceTimeoutMs?: number;
    __emitVoiceTranscript?: (transcript: string) => void;
    __spokenAgentReplies?: string[];
  }
}

let apiProcess: ChildProcess | null = null;
let apiDataDir: string | null = null;
const browserErrors = new WeakMap<Page, string[]>();
const externalRequests: string[] = [];
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1"]);

/** Registers the isolation hooks for the calling spec file. */
export function setupIsolatedApp() {
  test.beforeAll(async () => {
    apiPort = await new Promise<number>((resolve, reject) => {
      const server = createServer();
      server.once("error", reject);
      server.listen(0, "127.0.0.1", () => {
        const address = server.address();
        if (!address || typeof address === "string") {
          server.close();
          reject(new Error("Could not reserve an E2E API port."));
          return;
        }
        server.close(() => resolve(address.port));
      });
    });
    apiDataDir = await mkdtemp(join(tmpdir(), "pixel-e2e-"));
    apiProcess = spawn(
      venvPython(),
      ["-m", "uvicorn", "app.main:app", "--app-dir", join("apps", "api"), "--port", String(apiPort)],
      {
        cwd: process.cwd(),
        env: {
          ...process.env,
          PIXEL_DB_PATH: join(apiDataDir, "demo.sqlite3"),
          PIXEL_SYNTHETIC_DEMO: "true",
          PIXEL_DEMO_SEEDS: "true",
          // Paid providers are refused before dispatch, and any outbound call fails loudly.
          PIXEL_PAID_PROVIDERS_ENABLED: "false",
          PIXEL_BLOCK_EXTERNAL_HTTP: "true",
          // Behave identically on developer machines and CI: no local .env files or keys.
          PIXEL_IGNORE_ENV_FILES: "true",
          LLM_ENABLED: "false",
          // Every test gets a fresh private visitor instance; limits are covered by API tests.
          PIXEL_RATE_LIMITS: "off"
        },
        stdio: "ignore",
        windowsHide: true
      }
    );

    apiProcess.unref();
    await waitForApi();

  });

  test.afterAll(async () => {
    if (apiProcess?.exitCode === null) {
      const exited = new Promise<void>((resolve) => apiProcess!.once("exit", () => resolve()));
      apiProcess.kill();
      await Promise.race([exited, delay(5_000)]);
      if (apiProcess.exitCode === null) {
        apiProcess.kill("SIGKILL");
        await Promise.race([exited, delay(5_000)]);
      }
    }
    apiProcess = null;
    if (apiDataDir) {
      await rm(apiDataDir, { recursive: true, force: true, maxRetries: 3 });
      apiDataDir = null;
    }
  });

  test.afterEach(async ({ page, context }) => {
    // Moving between screens closes the assistant's conversation, and that request can still be
    // in flight when a test ends. Let it finish while the routes are installed: closing the
    // context first raced it, and a request that lost the race reached the sentinel as escaped.
    for (const open of context.pages()) {
      await open.waitForLoadState("networkidle", { timeout: 5_000 }).catch(() => undefined);
    }
    // Context routes remain installed until every page has stopped issuing requests.
    await context.close();
    const escaped = drainEscapedRequests();
    expect(escaped, "Requests escaped the isolated test backend").toEqual([]);
    expect(externalRequests.splice(0), "Browser contacted an external host").toEqual([]);
    expect(browserErrors.get(page) ?? [], "Unexpected browser runtime errors").toEqual([]);
  });

  test.beforeEach(async ({ page, context }) => {
    const errors: string[] = [];
    browserErrors.set(page, errors);
    page.on("pageerror", (error) => errors.push(error.message));
    await context.route((url) => !LOCAL_HOSTS.has(url.hostname), async (route) => {
      externalRequests.push(new URL(route.request().url()).hostname);
      await route.abort();
    });
    await context.route(agentApiRoute, forwardToFreshBackend);
  });
}

export async function openApp(page: Page) {
  const authDone = page.waitForResponse(
    (resp) => resp.url().includes("/visitor-sessions") && resp.status() === 200
  );
  const dataLoaded = page.waitForResponse(
    (resp) => new URL(resp.url()).pathname === "/api/agent/demo-data" && resp.status() === 200
  );
  await page.goto("/demo");
  await authDone;
  await dataLoaded;
  await expect(page.getByTestId("current-view-title")).toHaveText("Dashboard");
  await expect(page.getByTestId("chat-input")).toBeVisible();
  await expect(page.getByTestId("diagnostics-panel")).toBeHidden();
  await expect(page.getByTestId("session-summary")).toBeHidden();
  await expect(page.getByTestId("trace-status")).toBeHidden();
}

// Playwright errors that only mean nobody is waiting for the answer any more.
const ABANDONED_REQUEST_ERRORS = [
  "has been closed", // the page or context closed during teardown
  "has been disposed", // the page aborted the request, e.g. speech cancelled mid-flight
  "context disposed" // the browser context closed while the proxied request was running
];

export async function forwardToFreshBackend(route: Route) {
  try {
    const response = await route.fetch({ url: freshBackendUrl(route.request().url()) });
    await route.fulfill({ response });
  } catch (error) {
    if (!ABANDONED_REQUEST_ERRORS.some((message) => String(error).includes(message))) throw error;
  }
}

export function freshBackendUrl(requestUrl: string): string {
  const url = new URL(requestUrl);
  const backendPath = url.pathname.replace("/api/agent", "/api");
  return `http://127.0.0.1:${apiPort}${backendPath}${url.search}`;
}

export async function browserAuthHeaders(page: Page): Promise<Record<string, string>> {
  const token = await page.evaluate(() => {
    const key = Object.keys(window.sessionStorage)
      .find((candidate) => candidate.startsWith("pixel_demo_auth:"));
    return key ? window.sessionStorage.getItem(key) : null;
  });
  if (!token) throw new Error("The browser has no private demo token.");
  return { Authorization: `Bearer ${token}` };
}

async function waitForApi() {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (apiProcess?.exitCode !== null) {
      throw new Error(`E2E API server exited early with code ${apiProcess?.exitCode}`);
    }

    try {
      const response = await fetch(`http://127.0.0.1:${apiPort}/health`);
      if (response.ok) return;
    } catch {
      // Not listening yet.
    }
    await delay(500);
  }

  throw new Error(`Timed out waiting for E2E API server on port ${apiPort}.`);
}

export function delay(ms: number) {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

export function drainEscapedRequests() {
  const logPath = resolve(process.cwd(), E2E_SENTINEL_LOG);
  let contents = "";
  try {
    contents = readFileSync(logPath, "utf8");
  } catch {
    contents = "";
  }
  writeFileSync(logPath, "", "utf8");
  return contents.split(/\r?\n/).filter(Boolean);
}

export async function sendChat(page: Page, message: string) {
  await page.getByTestId("chat-input").fill(message);
  await page.getByTestId("chat-send").click();
  await expect(page.getByTestId("turn-status")).not.toHaveText("Thinking");
}

export async function installMockVoice(page: Page) {
  await page.addInitScript(() => {
    window.__demoVoiceSilenceTimeoutMs = 100;

    type MockRecognitionResult = {
      isFinal: boolean;
      0: {
        transcript: string;
      };
    };

    type MockRecognitionEvent = {
      resultIndex: number;
      results: {
        length: number;
        0: MockRecognitionResult;
      };
    };

    class MockSpeechRecognition {
      continuous = false;
      interimResults = false;
      lang = "en-US";
      onend: (() => void) | null = null;
      onerror: ((event: { error: string }) => void) | null = null;
      onresult: ((event: MockRecognitionEvent) => void) | null = null;
      onstart: (() => void) | null = null;

      start() {
        const testWindow = window as Window & { __activeRecognition?: MockSpeechRecognition };
        testWindow.__activeRecognition = this;
        window.setTimeout(() => this.onstart?.(), 0);
      }

      stop() {
        window.setTimeout(() => this.onend?.(), 0);
      }

      abort() {
        this.onend = null;
      }
    }

    const testWindow = window as Window & {
      SpeechRecognition?: typeof MockSpeechRecognition;
      webkitSpeechRecognition?: typeof MockSpeechRecognition;
      SpeechSynthesisUtterance?: new (text: string) => SpeechSynthesisUtterance;
      __activeRecognition?: MockSpeechRecognition;
      __activeUtterance?: SpeechSynthesisUtterance;
      __emitVoiceTranscript?: (transcript: string) => void;
    };

    Object.defineProperty(testWindow, "SpeechRecognition", {
      configurable: true,
      value: MockSpeechRecognition
    });
    Object.defineProperty(testWindow, "webkitSpeechRecognition", {
      configurable: true,
      value: MockSpeechRecognition
    });
    Object.defineProperty(testWindow, "speechSynthesis", {
      configurable: true,
      value: {
        getVoices: () => [],
        resume: () => undefined,
        cancel: () => {
          const utterance = testWindow.__activeUtterance;
          testWindow.__activeUtterance = undefined;
          utterance?.onend?.(new Event("end") as SpeechSynthesisEvent);
        },
        speak: (utterance: SpeechSynthesisUtterance) => {
          testWindow.__activeUtterance = utterance;
          window.setTimeout(() => utterance.onstart?.(new Event("start") as SpeechSynthesisEvent), 0);
        }
      }
    });
    Object.defineProperty(testWindow, "SpeechSynthesisUtterance", {
      configurable: true,
      value: function MockUtterance(this: SpeechSynthesisUtterance, text: string) {
        Object.defineProperty(this, "text", { configurable: true, value: text });
      }
    });

    testWindow.__emitVoiceTranscript = (transcript: string) => {
      const recognition = testWindow.__activeRecognition;
      if (!recognition) return;

      recognition.onresult?.({
        resultIndex: 0,
        results: {
          0: {
            0: { transcript },
            isFinal: true
          },
          length: 1
        }
      });
      recognition.onend?.();
    };
  });
}
