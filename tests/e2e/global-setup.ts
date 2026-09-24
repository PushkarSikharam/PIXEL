import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { E2E_PROCESS_FILE, E2E_SENTINEL_LOG, E2E_SENTINEL_PORT } from "./ports";

const WEB_PORT = 3100;

export default async function startWebServer() {
  await assertPortFree(WEB_PORT, "web server");
  await assertPortFree(E2E_SENTINEL_PORT, "sentinel");
  const sentinel = await startSentinel();
  const webRoot = resolve(process.cwd(), "apps/web");
  const nextBin = resolve(process.cwd(), "node_modules/next/dist/bin/next");
  const server = spawn(process.execPath, [nextBin, "dev", "--port", String(WEB_PORT)], {
    cwd: webRoot,
    env: {
      ...process.env,
      PIXEL_TEST_BUILD: "1",
      NEXT_PUBLIC_API_BASE_URL: "/api/agent",
      PIXEL_AGENT_API_BASE_URL: `http://127.0.0.1:${E2E_SENTINEL_PORT}/api`
    },
    stdio: "inherit",
    windowsHide: true
  });

  try {
    await waitForServer(server);
    writeProcessFile(server, sentinel);
  } catch (error) {
    await stopServer(server);
    await stopServer(sentinel);
    throw error;
  }
}

async function startSentinel() {
  const logPath = resolve(process.cwd(), E2E_SENTINEL_LOG);
  mkdirSync(dirname(logPath), { recursive: true });
  writeFileSync(logPath, "", "utf8");
  const sentinelScript = resolve(process.cwd(), "tests/e2e/sentinel-server.mjs");
  const sentinel = spawn(process.execPath, [sentinelScript, String(E2E_SENTINEL_PORT), logPath], {
    cwd: process.cwd(),
    stdio: "ignore",
    windowsHide: true
  });
  await waitForSentinel(sentinel);
  writeFileSync(logPath, "", "utf8");
  return sentinel;
}

async function waitForSentinel(sentinel: ChildProcess) {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    if (sentinel.exitCode !== null) {
      throw new Error(`E2E sentinel exited early with code ${sentinel.exitCode}.`);
    }
    try {
      const response = await fetch(`http://127.0.0.1:${E2E_SENTINEL_PORT}/health`);
      if (response.status === 503) return;
    } catch {
      // Not listening yet.
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 100));
  }
  throw new Error(`Timed out waiting for E2E sentinel on port ${E2E_SENTINEL_PORT}.`);
}

function writeProcessFile(server: ChildProcess, sentinel: ChildProcess) {
  const processFile = resolve(process.cwd(), E2E_PROCESS_FILE);
  mkdirSync(dirname(processFile), { recursive: true });
  writeFileSync(processFile, JSON.stringify({
    webPid: server.pid ?? null,
    sentinelPid: sentinel.pid ?? null
  }), "utf8");
}

async function assertPortFree(port: number, label: string) {
  try {
    await fetch(`http://127.0.0.1:${port}`);
  } catch {
    // Nothing is listening, which is what the e2e server needs.
    return;
  }
  throw new Error(
    `Port ${port} is already serving a ${label}. Stop the existing process before running e2e.`
  );
}

async function waitForServer(server: ChildProcess) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (server.exitCode !== null) {
      throw new Error(`E2E web server exited early with code ${server.exitCode}.`);
    }
    try {
      const response = await fetch(`http://127.0.0.1:${WEB_PORT}`);
      if (response.ok) return;
    } catch {
      // Not listening yet.
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 500));
  }
  await stopServer(server);
  throw new Error(`Timed out waiting for E2E web server on port ${WEB_PORT}.`);
}

async function stopServer(server: ChildProcess) {
  if (server.exitCode !== null || !server.pid) return;

  if (process.platform === "win32") {
    spawnSync("taskkill", ["/pid", String(server.pid), "/T", "/F"], {
      stdio: "ignore",
      windowsHide: true
    });
    return;
  }

  const exited = new Promise<void>((resolveExit) => server.once("exit", () => resolveExit()));
  server.kill("SIGTERM");
  await Promise.race([
    exited,
    new Promise((resolveDelay) => setTimeout(resolveDelay, 5_000))
  ]);
  if (server.exitCode === null) server.kill("SIGKILL");
}
