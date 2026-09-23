import { expect, test } from "@playwright/test";
import { setupIsolatedApp } from "./harness";

setupIsolatedApp();

test("system entry uses the designed console and keeps demo chat separate", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/console$/);
  await expect(page.getByRole("heading", { name: "Overview", exact: true })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Primary" })).toBeVisible();
  await expect(page.getByText("Design preview with sample data.", { exact: false })).toBeVisible();
  await expect(page.getByText("LIVE DEMO GUIDE", { exact: false })).toHaveCount(0);
  await page.screenshot({ path: "test-results/system-desktop.png", fullPage: true });
  await page.getByRole("link", { name: "Products", exact: true }).first().click();
  await expect(page).toHaveURL(/\/console\/products$/);
  await page.getByRole("link", { name: "Visit demo", exact: true }).click();
  await expect(page).toHaveURL(/\/demo$/);
  await expect(page.getByText("LIVE DEMO GUIDE", { exact: false })).toBeVisible();
});

test("designed console fits a mobile viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/console");
  await expect(page.getByRole("heading", { name: "Overview", exact: true })).toBeVisible();
  await expect(page.getByText("LIVE DEMO GUIDE", { exact: false })).toHaveCount(0);
  await page.screenshot({ path: "test-results/system-mobile.png", fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
