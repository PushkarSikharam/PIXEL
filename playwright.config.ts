import { defineConfig, devices } from "@playwright/test";

const webPort = 3100;
const webBaseUrl = `http://localhost:${webPort}`;

export default defineConfig({
  // Core browser tests, plus each product package's own browser tests.
  testDir: ".",
  testMatch: ["tests/e2e/**/*.spec.ts", "products/*/tests/**/*.spec.ts"],
  testIgnore: [".worktrees/**", "**/.pytest_cache/**", "tmp-*.ts", "playwright-report/**", "test-results/**"],
  timeout: 30_000,
  expect: {
    timeout: 10_000
  },
  fullyParallel: false,
  // Every spec file starts its own isolated API and the fixed-port sentinel.
  workers: 1,
  globalSetup: "./tests/e2e/global-setup.ts",
  // In CI, the github reporter turns each failure into a public annotation on the run.
  reporter: process.env.CI
    ? [["github"], ["list"], ["html", { open: "never" }]]
    : [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: webBaseUrl,
    trace: "on-first-retry"
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] }
    }
  ]
});
