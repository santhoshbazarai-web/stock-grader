// Financials, Key Metrics, Fair Value, Dividends and Compare tabs.
//  - demo data (python -m app.devtools.demo): behaviour checks on DEMOTECH / DEMOBANK
//  - SHOTS=1 on the acceptance data (backend/tests/seed_screenshots.py): one screenshot per tab
//    for HDFCBANK (bank layout) and TCS (non-bank) in docs/screenshots/tab-*.png
import { expect, test, type Page } from "@playwright/test";

import { login } from "./helpers";

const unstick = (page: Page) => page.addStyleTag({ content: "nav[aria-label=Main],header{position:static !important}" });
const TABS = [
  ["financials", "Financials"],
  ["key-metrics", "Key Metrics"],
  ["fair-value", "Fair Value"],
  ["dividends", "Dividends"],
  ["compare", "Compare"],
] as const;

test.describe("tabs on demo data", () => {
  test.skip(!!process.env.SHOTS, "screenshot run uses the acceptance data");

  test("financials: toggle, YoY rows, source badges; bank layout", async ({ page }) => {
    await login(page, "/stocks/DEMOTECH");
    await page.getByRole("tab", { name: "Financials" }).click();
    const income = page.getByRole("table", { name: "Income statement" });
    await expect(income).toContainText("Revenue");
    await expect(income).toContainText("YoY %");
    await expect(page.getByRole("table", { name: "Balance sheet" })).toBeVisible();
    await expect(page.getByRole("table", { name: "Cash flow" })).toBeVisible();
    await page.getByRole("button", { name: "quarterly" }).click();
    await expect(page.getByRole("table", { name: "Balance sheet" })).toHaveCount(0);
    await page.goto("/stocks/DEMOBANK#financials");
    await expect(page.getByRole("table", { name: "Income statement" })).toContainText("Net interest income");
    await expect(page.getByRole("table", { name: "Income statement" })).toContainText("Provisions");
  });

  test("key metrics: groups with median and percentile; bank groups", async ({ page }) => {
    await login(page, "/stocks/DEMOTECH");
    await page.getByRole("tab", { name: "Key Metrics" }).click();
    for (const g of ["Profitability", "Growth", "Financial strength", "Efficiency", "Valuation ratios", "Per-share data"]) {
      await expect(page.getByRole("region", { name: g })).toBeVisible();
    }
    await expect(page.getByRole("region", { name: "Profitability" })).toContainText("5-yr median");
    await page.goto("/stocks/DEMOBANK#key-metrics");
    await expect(page.getByRole("region", { name: "Asset quality" })).toContainText("Gross NPA");
  });

  test("fair value: football field, methods, scenarios, assumptions drawer", async ({ page }) => {
    await login(page, "/stocks/DEMOTECH");
    await page.getByRole("tab", { name: "Fair Value" }).click();
    await expect(page.getByRole("img", { name: /Football field/ })).toBeVisible();
    await expect(page.getByRole("table", { name: "Valuation methods" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Why this zone" })).toBeVisible();
    await page.getByRole("button", { name: "Edit assumptions" }).click();
    await expect(page.getByRole("dialog", { name: "Assumptions" })).toBeVisible();
    await page.getByRole("button", { name: "Close assumptions" }).click();
    await expect(page.getByRole("dialog", { name: "Assumptions" })).toHaveCount(0);
  });

  test("dividends: missing records are explained, never zero", async ({ page }) => {
    await login(page, "/stocks/DEMOTECH");
    await page.getByRole("tab", { name: "Dividends" }).click();
    await expect(page.getByRole("region", { name: "Yield and growth" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Upcoming ex-dates" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Bonus and split history" })).toBeVisible();
  });

  test("compare: peers suggested, add one, table and charts", async ({ page }) => {
    await login(page, "/stocks/DEMOTECH");
    await page.getByRole("tab", { name: "Compare" }).click();
    await expect(page.getByRole("table", { name: "Comparison" })).toContainText("DEMOTECH");
    const peer = page.getByLabel("Suggested peers").getByRole("button").first();
    await peer.click();
    await expect(page.getByRole("table", { name: "Comparison" }).locator("thead th")).toHaveCount(3);
    await expect(page.getByRole("figure", { name: "Normalised price chart" })).toBeVisible();
    await expect(page.getByRole("figure", { name: "Pillar radar" })).toBeVisible();
  });
});

test.describe("tab screenshots", () => {
  test.skip(!process.env.SHOTS, "set SHOTS=1 (acceptance data from backend/tests/seed_screenshots.py)");

  for (const sym of ["HDFCBANK", "TCS"] as const) {
    test(`${sym} tabs`, async ({ page }) => {
      await login(page, `/stocks/${sym}`);
      for (const [id, label] of TABS) {
        await page.getByRole("tab", { name: label }).click();
        if (id === "compare") {
          const peer = page.getByLabel("Suggested peers").getByRole("button").first();
          if (await peer.count()) await peer.click();
          else {
            await page.getByLabel("Add a stock to compare").fill(sym === "TCS" ? "HDFCBANK" : "TCS");
            await page.getByLabel("Search results").getByRole("button").first().click();
          }
          await expect(page.getByRole("figure", { name: "Pillar radar" })).toBeVisible();
        }
        await page.waitForTimeout(1200); // charts + async cards settle
        await unstick(page);
        await page.screenshot({ path: `../docs/screenshots/tab-${sym.toLowerCase()}-${id}.png`, fullPage: true });
      }
    });
  }
});
