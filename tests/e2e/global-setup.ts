import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { resolve } from "node:path";
import { E2E_SENTINEL_PORT } from "./ports";

const WEB_PORT = 3100;

export default async function startWebServer() {
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

  await waitForServer(server);
  return async () => stopServer(server);
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
