// On-demand pipeline (SPEC §3.7) with live progress over Server-Sent Events. Needs the
// pipeline worker running next to the API: python -m app.jobs pipeline-worker
import { expect, test } from "@playwright/test";

import { login } from "./helpers";

test("refresh runs the pipeline with live progress and swaps in the new report", async ({
  page,
}) => {
  test.setTimeout(180_000);
  await login(page, "/stocks/DEMOTECH");
  await expect(page.getByRole("heading", { name: "DEMOTECH" })).toBeVisible();
  await page.getByRole("button", { name: "Refresh data" }).click();

  const panel = page.getByRole("region", { name: "Pipeline for DEMOTECH" });
  await expect(panel).toBeVisible();
  await panel.getByRole("button", { name: "Show steps" }).click();
  const steps = panel.getByRole("list", { name: "Pipeline steps" });
  await expect(steps.getByRole("listitem")).toHaveCount(13);
  // progress arrives step by step, then the run ends
  await expect(panel).toContainText("Report updated", { timeout: 150_000 });
  await expect(panel.getByRole("progressbar")).toHaveAttribute(
    "aria-valuenow",
    "13",
  );
  await expect(
    steps.getByRole("listitem", { name: "Report: ok" }),
  ).toBeVisible();
  await expect(
    steps.getByRole("listitem", { name: "Valuation: ok" }),
  ).toContainText("fair value ₹");
  // demo stocks have no exchange XBRL figures: nothing to reconcile (SPEC §3.9)
  await expect(
    steps.getByRole("listitem", { name: "Reconciliation: ok" }),
  ).toContainText("no exchange-filed figures to check");
  // optional steps without data (no network in the test stack) are warnings …
  await expect(
    steps.getByRole("listitem", { name: "Filings index: warning" }),
  ).toContainText("NSE results list unavailable");
  // … and the rebuilt report lists them as data gaps
  const gaps = page
    .locator("details")
    .filter({ hasText: "Inputs the report ran without" });
  if ((await gaps.getAttribute("open")) === null)
    await gaps.locator("summary").click();
  await expect(
    page.getByText("pipeline filings index: NSE results list unavailable"),
  ).toBeVisible();
});

test("an unknown symbol fails at the symbol step", async ({ page }) => {
  await login(page, "/stocks/NOSUCHCO");
  const panel = page.getByRole("region", { name: "Pipeline for NOSUCHCO" });
  await expect(panel).toContainText(
    "Failed: Symbol: NOSUCHCO is not an NSE symbol",
    { timeout: 60_000 },
  );
  await expect(
    panel.getByRole("listitem", { name: "Prices: skipped" }),
  ).toBeVisible();
});
