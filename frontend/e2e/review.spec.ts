// Annual-report PDF gap filler (SPEC v0.2 §3.6 steps 3-4): upload a report, review the
// low-confidence values in one click, and see the years it fills in the stock page's coverage grid.
import path from "node:path";

import { expect, test } from "@playwright/test";

import { login } from "./helpers";

const fixtures = path.resolve(__dirname, "../../backend/tests/fixtures/annual_reports");

async function upload(form: import("@playwright/test").Locator, file: string, fy: string, published: string) {
  await form.locator("input[type=file]").setInputFiles(path.join(fixtures, file));
  await form.getByLabel("Report symbol").fill("DEMOSOFT");
  await form.getByLabel("Fiscal year").fill(fy);
  await form.getByLabel("Published on").fill(published);
  await form.getByRole("button", { name: "Read report" }).click();
}

test("annual reports: upload, review queue, coverage grid", async ({ page }) => {
  await login(page, "/review");
  const form = page.getByRole("form", { name: "Upload annual report" });

  // An Indian GAAP report in Rs. lakhs: a bare "TOTAL" row is a weak label → review queue.
  await upload(form, "acme_ar_fy2012_igaap.pdf", "2012", "2012-08-01");
  const outcome = page.getByRole("status").filter({ hasText: "DEMOSOFT FY2012: read" });
  await expect(outcome).toContainText("standalone balance sheet (p.2), standalone cash flow (p.3)");
  await expect(page.getByRole("table", { name: "Annual reports" })).toContainText("DEMOSOFT");

  await page.getByLabel("Filter symbol").fill("DEMOSOFT");
  // include decided values too, so a re-run against the same database finds its rows
  await page.getByRole("button", { name: "Accepted", exact: true }).click();
  await page.getByRole("button", { name: "Corrected", exact: true }).click();
  const queue = page.getByRole("table", { name: "Review queue" });
  const fy12 = queue.getByRole("row", { name: "DEMOSOFT total_assets 2012-03-31" });
  const fy11 = queue.getByRole("row", { name: "DEMOSOFT total_assets 2011-03-31" });
  await expect(fy12).toContainText("“TOTAL”");
  await expect(fy12).toContainText("page 2");
  await expect(fy12).toContainText("790.00");

  // why: the confidence expands to its reasons
  await fy12.getByRole("button", { name: "85%" }).click();
  await expect(fy12).toContainText("label weight 0.85");

  const accept = fy12.getByRole("button", { name: "Accept" });
  if (await accept.count()) await accept.click();
  await expect(fy12).toContainText("Accepted · stored");

  const save = fy11.getByRole("button", { name: "Save" });
  if (await save.count()) {
    await fy11.getByLabel(/Corrected value for total_assets 2011-03-31/).fill("741.5");
    await save.click();
  }
  await expect(fy11).toContainText("Corrected · stored");
  await expect(fy11).toContainText("741.50");

  // An Ind AS report (₹ crore): every value is confident and stored straight away …
  await upload(form, "acme_ar_fy2024.pdf", "2024", "2024-06-11");
  await expect(page.getByRole("status").filter({ hasText: "DEMOSOFT FY2024: read" })).toContainText(
    /\d+ values stored, 0 to review/,
  );

  // … and fills FY2024 / FY2023 balance sheet and cash flow on the stock page
  await page.goto("/stocks/DEMOSOFT");
  const grid = page.getByRole("table", { name: "Data coverage, consolidated" });
  await expect(grid).toBeVisible({ timeout: 60_000 });
  await expect(grid.getByLabel(/^BS FY2024: Annual-report PDF/)).toBeVisible();
  await expect(grid.getByLabel(/^CF FY2023: Annual-report PDF/)).toBeVisible();
  await expect(page.getByRole("list", { name: "Coverage legend" })).toContainText("PDF = Annual-report PDF");
});
