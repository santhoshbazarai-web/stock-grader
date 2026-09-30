// Dashboard, screener, watchlist & alerts, settings (SPEC §9). Needs the demo data
// (python -m app.devtools.demo). Never saves config: the dev server edits the real config/.
import path from "node:path";

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

  // Kite is code-complete but disabled in providers.yaml (SPEC §0): a Disabled card, no Connect
  const cards = page.getByRole("list", { name: "Broker connections" });
  await expect(cards.getByRole("listitem", { name: "Zerodha Kite: Disabled" })).toContainText(
    "brokers.kite.enabled",
  );
  await expect(page.getByRole("button", { name: /Zerodha Kite/ })).toHaveCount(0);

  const connect = page.getByRole("button", { name: /^(Connect Fyers|Reconnect)$/ });
  test.skip(!(await connect.isVisible()), "Fyers API credentials not configured on this stack");
  await page.route("https://api-t1.fyers.in/**", (r) => r.fulfill({ status: 200, body: "fyers login stub" }));
  await connect.click();
  await expect(page).toHaveURL(/^https:\/\/api-t1\.fyers\.in\/api\/v3\/generate-authcode\?.*client_id=/);
});

test("reconnect banner while Fyers has no valid token", async ({ page }) => {
  await login(page, "/");
  const status = await (await page.request.get("/api/brokers/status")).json();
  const fyers = status.find((b: { broker: string }) => b.broker === "fyers");
  test.skip(!fyers.configured || fyers.connected, "needs Fyers configured and not connected");
  const banner = page.getByRole("complementary", { name: "Broker token" });
  await expect(banner).toContainText("Fyers is not connected");
  await banner.getByRole("link", { name: "Reconnect" }).click();
  await expect(page).toHaveURL(/\/settings$/);
  await expect(page.getByRole("complementary", { name: "Broker token" })).toHaveCount(0);
});

test("uploads list shows stored Screener datasets", async ({ page }) => {
  await login(page, "/settings");
  await expect(page.getByRole("table", { name: "Uploaded fundamentals" })).toContainText("DEMOIT");
});

test("XBRL results filings: upload, per-file outcome, ledger filter", async ({ page }) => {
  const fixtures = path.resolve(__dirname, "../../backend/tests/fixtures/xbrl");
  await login(page, "/settings");
  const form = page.getByRole("form", { name: "Upload XBRL filings" });
  await form.locator("input[type=file]").setInputFiles([
    path.join(fixtures, "acme_q4fy24_consolidated.xml"),
    path.join(fixtures, "acme_q2fy25_standalone.xml"),
  ]);
  await form.getByLabel("XBRL symbol").fill("ACME");
  await form.getByRole("button", { name: "Upload filings" }).click();
  const outcome = page.getByRole("status").filter({ hasText: "ACME: 2 of 2 stored" });
  await expect(outcome).toContainText("Q 2024-03-31 + FY 2024-03-31, consolidated, usable from 2024-05-11");
  await expect(outcome).toContainText("Q 2024-09-30, standalone, usable from 2024-10-25");

  // a document for another company is refused, file by file
  await form.locator("input[type=file]").setInputFiles(path.join(fixtures, "acme_q4fy24_consolidated.xml"));
  await form.getByLabel("XBRL symbol").fill("DEMOIT");
  await form.getByRole("button", { name: "Upload filings" }).click();
  await expect(page.getByRole("status").filter({ hasText: "DEMOIT: 0 of 1 stored" })).toContainText(
    "failed: document is for ACME, not DEMOIT",
  );

  const table = page.getByRole("table", { name: "Results filings" });
  await page.getByRole("button", { name: "Stored", exact: true }).click();
  await expect(table).toContainText("ACME");
  await expect(table).not.toContainText("Failed");
  await expect(page.getByLabel("Filings summary")).toContainText(/filings? stored for/);
});

test("notifications: test message reaches the bell, which marks it read", async ({ page }) => {
  await login(page, "/settings");
  await page.getByRole("button", { name: "Send test notification" }).click();
  await expect(page.getByRole("status").filter({ hasText: /Test (sent|notification created)/ })).toBeVisible();
  const bell = page.getByRole("button", { name: /Notifications, \d+ unread/ });
  await expect(bell).toBeVisible(); // refreshed immediately, not on the next poll
  await bell.click();
  const panel = page.getByRole("dialog", { name: "Notifications" });
  await expect(panel).toContainText("Stock Grader test notification");
  await panel.getByRole("button", { name: "Mark all read" }).click();
  await expect(page.getByRole("button", { name: "Notifications", exact: true })).toBeVisible();
});
