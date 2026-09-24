import { defineConfig, devices } from "@playwright/test";

const webPort = 3100;
const webBaseUrl = `http://localhost:${webPort}`;

export default defineConfig({
  timeout: 30_000,
  expect: {
    timeout: 10_000
  },
  fullyParallel: false,
  // Every spec file starts its own isolated API and the fixed-port sentinel.
  workers: 1,
  globalSetup: "./tests/e2e/global-setup.ts",
  globalTeardown: "./tests/e2e/global-teardown.ts",
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
      name: "chromium-core",
      testDir: "tests/e2e",
      testMatch: ["**/*.spec.ts"],
      testIgnore: ["**/.pytest_cache/**", "tmp-*.ts", "playwright-report/**", "test-results/**"],
      use: { ...devices["Desktop Chrome"] }
    },
    {
      name: "chromium-products",
      testDir: "products",
      testMatch: ["*/tests/**/*.spec.ts"],
      testIgnore: ["**/__pycache__/**", "**/.pytest_cache/**", "tmp-*.ts", "playwright-report/**", "test-results/**"],
      use: { ...devices["Desktop Chrome"] }
    }
  ]
});
