import { expect, test } from "@playwright/test";
import { setupIsolatedApp } from "./harness";

setupIsolatedApp();

test("system entry starts with sign-in and keeps private navigation hidden", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/sign-in$/);
  await expect(page.getByRole("heading", { name: "Sign in to Pixel", exact: true })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Primary" })).toHaveCount(0);
  await expect(page.getByText("LIVE DEMO GUIDE", { exact: false })).toHaveCount(0);
  await page.screenshot({ path: "test-results/system-desktop.png", fullPage: true });
  await page.getByRole("link", { name: "Explore the demo", exact: true }).click();
  await expect(page).toHaveURL(/\/demo$/);
  await expect(page.getByText("LIVE DEMO GUIDE", { exact: false })).toBeVisible();
});

test("signed-out entry fits a mobile viewport without private navigation", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in to Pixel", exact: true })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Primary" })).toHaveCount(0);
  await expect(page.getByText("LIVE DEMO GUIDE", { exact: false })).toHaveCount(0);
  await page.screenshot({ path: "test-results/system-mobile.png", fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
