// Dashboard, screener, watchlist & alerts, settings (SPEC §9). Needs the demo data
// (python -m app.devtools.demo). Never saves config: the dev server edits the real config/.
import { expect, test } from "@playwright/test";

import { login } from "./helpers";

test("dashboard shows brokers, freshness, buy-zone list and alerts", async ({ page }) => {
  await login(page, "/");
  await expect(page.getByRole("navigation", { name: "Main" })).toContainText("Screener");
  await expect(page.getByLabel("Broker connections")).toContainText("Fyers");
  await expect(page.getByLabel("Broker connections")).toContainText("Zerodha Kite");
  await expect(page.getByText("Data freshness")).toBeVisible();
  await expect(page.getByText(/open data gaps/)).toBeVisible();
  await expect(page.getByText("A-grade stocks at their buy zone")).toBeVisible();
  await expect(page.getByText("Triggered alerts")).toBeVisible();
});

test("screener filters live in the URL, columns sort, presets save and load", async ({ page }) => {
  await login(page, "/screener");
  const table = page.getByRole("table", { name: "Screener results" });
  await expect(table.locator("tbody tr").first()).toBeVisible();
  const all = await table.locator("tbody tr").count();

  await page.getByRole("button", { name: "Extreme Premium" }).click();
  await expect(page).toHaveURL(/zone=extreme_premium/);
  await expect(table.locator("tbody tr")).not.toHaveCount(all);
  for (const zone of await table.locator("tbody tr td:nth-child(4)").allInnerTexts()) {
    expect(zone).toBe("Extreme Premium");
  }

  await page.getByRole("button", { name: "Symbol" }).click();
  await expect(page).toHaveURL(/sort=symbol/);
  const symbols = await table.locator("tbody tr td:first-child a").allInnerTexts();
  expect(symbols).toEqual([...symbols].sort().reverse()); // first click = descending
  await page.getByRole("button", { name: "Symbol" }).click();
  await expect(page).toHaveURL(/order=asc/);

  const name = `e2e preset ${Date.now()}`;
  await page.getByLabel("Preset name").fill(name);
  await page.getByRole("button", { name: "Save preset" }).click();
  await expect(page.getByRole("status")).toContainText(`Saved “${name}”`);

  await page.getByRole("button", { name: "Reset", exact: true }).click();
  await expect(page).toHaveURL(/\/screener$/);
  await page.getByLabel("Preset", { exact: true }).selectOption(name);
  await expect(page).toHaveURL(/zone=extreme_premium/);
  await expect(page).toHaveURL(/sort=symbol/);
  await page.getByRole("button", { name: "Delete" }).click();
  await expect(page.getByRole("status")).toContainText(`Deleted “${name}”`);
});

test("watchlist and alerts: add, toggle, remove", async ({ page }) => {
  await login(page, "/watchlist");
  await page.getByLabel("Symbol", { exact: true }).fill("demotech");
  await page.getByLabel("Notes").fill("e2e note");
  await page.getByRole("button", { name: "Add" }).click();
  const watch = page.getByRole("table", { name: "Watchlist" });
  await expect(watch).toContainText("DEMOTECH");
  await expect(watch).toContainText("e2e note");
  await page.getByRole("button", { name: "Remove DEMOTECH" }).click();
  await expect(page.getByRole("table", { name: "Watchlist" }).or(page.getByText("Your watchlist is empty."))).not.toContainText("DEMOTECH");

  await page.getByLabel("Alert symbol").fill("DEMOFMCG");
  await page.getByLabel("Alert type").selectOption("crosses_top_band");
  await page.getByRole("button", { name: "Create alert" }).click();
  const row = page.getByRole("table", { name: "Alerts" }).locator("tr", { hasText: "DEMOFMCG" });
  await expect(row).toContainText("Price crosses the top band");
  await row.getByRole("checkbox").click(); // controlled: flips after the server round-trip
  await expect(row).toContainText("paused");
  await row.getByRole("button", { name: /Delete alert/ }).click();
  await expect(page.getByRole("table", { name: "Alerts" }).locator("tr", { hasText: "DEMOFMCG" })).toHaveCount(0);
});

test("config editor validates as you type and blocks invalid saves", async ({ page }) => {
  await login(page, "/settings");
  const editor = page.getByLabel("scoring.yaml");
  await expect(editor).toContainText("weights:");
  const original = await editor.inputValue();
  await editor.fill(original.replace("quality: 25", "quality: 30"));
  await expect(page.locator("pre[role=alert]")).toContainText("scoring.yaml is invalid");
  await expect(page.locator("pre[role=alert]")).toContainText("sum to 100");
  await expect(page.getByRole("button", { name: "Save scoring.yaml" })).toBeDisabled();

  await editor.fill(original.replace("momentum_entry_min: 6", "momentum_entry_min: 7"));
  await expect(page.getByText("✓ Valid — ready to save")).toBeVisible();
  await expect(page.getByRole("button", { name: "Save scoring.yaml" })).toBeEnabled();
  await page.getByRole("button", { name: "Revert" }).click();
  await expect(editor).toHaveValue(original);
  await expect(page.getByText("Saved version")).toBeVisible();

  await page.getByRole("tab", { name: "jobs.yaml" }).click();
  await expect(page.getByLabel("jobs.yaml")).toContainText("schedules:");
});

test("broker Connect goes through the OAuth login; callbacks show a banner", async ({ page }) => {
  await login(page, "/settings?broker=fyers&status=connected");
  await expect(page.getByRole("status").filter({ hasText: "Fyers connected." })).toBeVisible();
  await page.goto("/settings?broker=kite&status=error&reason=login_declined");
  await expect(page.getByText("Zerodha Kite not connected: the login was cancelled at the broker.")).toBeVisible();

  await expect(page.getByLabel("Broker connections")).toContainText("Zerodha Kite");
  const connect = page.getByRole("button", { name: "Connect Zerodha Kite" });
  test.skip(!(await connect.isVisible()), "Kite API credentials not configured on this stack");
  await page.route("https://kite.zerodha.com/**", (r) => r.fulfill({ status: 200, body: "kite login stub" }));
  await connect.click();
  await expect(page).toHaveURL(/^https:\/\/kite\.zerodha\.com\/connect\/login\?.*api_key=/);
});

test("uploads list shows stored Screener datasets", async ({ page }) => {
  await login(page, "/settings");
  await expect(page.getByRole("table", { name: "Uploaded fundamentals" })).toContainText("DEMOIT");
});
