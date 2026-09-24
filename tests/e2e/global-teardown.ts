import { spawnSync } from "node:child_process";
import { readFileSync, rmSync } from "node:fs";
import { resolve } from "node:path";
import { E2E_PROCESS_FILE } from "./ports";

export default async function stopE2EProcesses() {
  const processFile = resolve(process.cwd(), E2E_PROCESS_FILE);
  let pids: Array<number | null> = [];
  try {
    const stored = JSON.parse(readFileSync(processFile, "utf8"));
    pids = [stored.webPid, stored.sentinelPid];
  } catch {
    pids = [];
  }

  for (const pid of pids) {
    if (!pid) continue;
    if (process.platform === "win32") {
      spawnSync("taskkill", ["/pid", String(pid), "/T", "/F"], {
        stdio: "ignore",
        windowsHide: true
      });
    } else {
      try {
        process.kill(pid, "SIGTERM");
      } catch {
        // Already gone.
      }
    }
  }

  try {
    rmSync(processFile, { force: true });
  } catch {
    // Best-effort cleanup only.
  }
}
