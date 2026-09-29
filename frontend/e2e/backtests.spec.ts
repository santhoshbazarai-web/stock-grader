// Backtests (SPEC §9, §11). Needs the demo data (python -m app.devtools.demo). Queued runs are
// executed by the worker's `backtests` job; set E2E_BACKTEST_CMD to a command that runs it
// once (e.g. "cd ../backend && uv run python -m app.jobs run backtests") to test the results
// view without waiting for the worker's schedule.
import { execSync } from "node:child_process";

import { expect, test } from "@playwright/test";

import { login } from "./helpers";

const DEMO = "DEMOBANK, DEMOCODE, DEMOFMCG, DEMOIT, DEMOSOFT, DEMOTECH";

/** Fill and submit the form (on /backtests, signed in); returns the new backtest's id. */
async function queue(page: import("@playwright/test").Page, holding: string) {
  const form = page.getByRole("form", { name: "New backtest" });
  await form.getByRole("button", { name: "Fair", exact: true }).click(); // add Fair to the defaults
  await expect(form.getByRole("button", { name: "Fair", exact: true })).toHaveAttribute("aria-pressed", "true");
  await form.getByLabel("Holding period (sessions)").fill(holding);
  await form.getByLabel("Start").fill("2021-01-01");
  await form.getByLabel("End").fill("2024-06-14");
  await form.getByLabel(/Symbols/).fill(DEMO);
  await form.getByRole("button", { name: "Run backtest" }).click();
  await expect(page).toHaveURL(/\/backtests\/\d+$/);
  return Number(page.url().split("/").pop());
}

test("form validates, queues a run and lists it in the history", async ({ page }) => {
  await login(page, "/backtests");
  const form = page.getByRole("form", { name: "New backtest" });
  await form.getByLabel("Holding period (sessions)").fill("2");
  await form.getByRole("button", { name: "Run backtest" }).click();
  await expect(form.getByRole("alert")).toContainText("Holding period must be 5–2520 sessions");
  await form.getByLabel("Holding period (sessions)").fill("60");
  await form.getByLabel(/Symbols/).fill("TCS, bad sym!");
  await form.getByRole("button", { name: "Run backtest" }).click();
  await expect(form.getByRole("alert")).toContainText("Not a valid symbol: SYM!");

  await page.reload();
  const id = await queue(page, "90");
  await expect(page.getByRole("heading", { name: `Backtest #${id}` })).toBeVisible();
  await expect(page.getByText("A+, A in Deep Discount, Discount, Fair · hold 90 sessions")).toBeVisible();
  await page.getByRole("link", { name: "← All backtests" }).click();
  const row = page.getByRole("table", { name: "Backtest history" }).getByRole("row").filter({ hasText: "hold 90 sessions" });
  await expect(row.first()).toContainText("6 symbols");
});

test("results show metrics vs Nifty 500, the equity curve and the grade × zone table", async ({ page }) => {
  test.skip(!process.env.E2E_BACKTEST_CMD, "set E2E_BACKTEST_CMD to run the backtests job");
  await login(page, "/backtests");
  const id = await queue(page, "120");
  await expect(page.getByRole("progressbar", { name: "Backtest progress" })).toBeVisible();
  execSync(process.env.E2E_BACKTEST_CMD!, { stdio: "inherit", timeout: 120_000 });

  // The page polls until the run is done.
  const metrics = page.getByLabel("Headline metrics");
  await expect(metrics).toContainText("CAGR", { timeout: 30_000 });
  await expect(metrics).toContainText(/Nifty 500 [+-]\d/);
  await expect(metrics).toContainText("Max drawdown");
  await expect(metrics).toContainText("Hit rate");
  await expect(page.getByRole("img", { name: /Equity curve chart/ })).toBeVisible();
  await expect(page.locator(".recharts-line")).toHaveCount(2); // portfolio + benchmark

  await page.getByRole("button", { name: "Table" }).click();
  const curve = page.getByRole("table", { name: "Equity curve" });
  await expect(curve.locator("tbody tr").first()).toContainText("100");

  const cells = page.getByRole("table", { name: "Results by grade and zone" });
  await expect(cells.locator("tbody tr")).toHaveCount(5);
  await expect(cells.locator("tbody td")).toHaveCount(25);
  await expect(cells.locator("td[title*='in your rules']").first()).toBeVisible();
  await expect(page.getByLabel("Caveats")).toContainText("survivorship bias");

  await page.getByText(/^Trades \(last \d+\)$/).click();
  await expect(page.getByRole("table", { name: "Trades" }).locator("tbody tr").first()).toBeVisible();

  await page.goto("/backtests");
  const row = page.getByRole("table", { name: "Backtest history" }).getByRole("row").filter({ has: page.getByRole("link", { name: String(id), exact: true }) });
  await expect(row).toContainText("Done");
  await expect(row).toContainText(/[+-]\d+\.\d%/);
});
