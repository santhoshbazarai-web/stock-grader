import { expect, test } from "@playwright/test";

import { login } from "./helpers";

test("login gate rejects a wrong password", async ({ page }) => {
  await page.goto("/stocks/DEMOIT");
  await page.fill("#password", "wrong");
  await page.click("button[type=submit]");
  await expect(page.locator("form [role=alert]")).toHaveText("wrong password");
});

test("report page renders every SPEC §9 section", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  await login(page, "/stocks/DEMOIT");

  const tab = (name: string) => page.getByRole("tab", { name, exact: true }).click();
  await expect(page.getByRole("heading", { name: "DEMOIT" })).toBeVisible();
  await expect(page.getByText(/^(A\+|A|B|C|D)$/).first()).toBeVisible();
  await expect(page.locator("[data-testid=price-chart] canvas").first()).toBeVisible();
  await expect(page.getByLabel("Chart legend")).toContainText("30-wk SMA");
  await expect(page.getByLabel("Chart legend")).toContainText("Demand zone");
  await expect(page.getByLabel("Pillar scores")).toBeVisible();
  await expect(page.getByLabel("Price ladder")).toBeVisible();
  await expect(page.getByTestId("overview-tables")).toBeVisible();
  await expect(page.getByText("Red flags", { exact: true })).toBeVisible();
  await expect(page.getByText(/Data gaps/)).toBeVisible();

  await tab("Fair Value");
  await expect(page.getByLabel("Valuation zone gauge")).toContainText("CMP ₹");
  await expect(page.getByLabel("Valuation zone gauge")).toContainText("FV ₹");
  await expect(page.getByRole("table", { name: "DCF sensitivity" })).toBeVisible();
  await expect(page.getByRole("table", { name: "DCF sensitivity" }).locator("td").first()).toBeVisible();
  await tab("Key Metrics");
  await expect(page.getByRole("img", { name: "Pillar scores radar" })).toBeVisible();
  await tab("Ownership & Events");
  await expect(page.locator("figcaption", { hasText: "Shareholding" }).first()).toBeVisible();
  await tab("Financials");
  for (const title of ["Sales", "EBITDA", "PAT", "Free cash flow", "ROCE", "Cash conversion cycle"]) {
    await expect(page.locator("figcaption", { hasText: title }).first()).toBeVisible();
  }

  await page.getByRole("button", { name: "6M" }).click();
  await expect(page.getByRole("button", { name: "6M" })).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("[data-testid=price-chart] canvas").first()).toBeVisible();

  await page.getByRole("button", { name: "Table" }).click();
  await expect(page.getByLabel("Fundamentals by year")).toContainText("FY24");
  expect(errors).toEqual([]);
});

test("saving an assumption recomputes the report, clearing restores it", async ({ page }) => {
  await login(page, "/stocks/DEMOIT");
  await page.getByRole("tab", { name: "Fair Value", exact: true }).click();
  const form = page.getByLabel("Valuation assumptions");
  await form.locator("input[name=wacc]").fill("10");
  await form.getByRole("button", { name: "Save & recompute" }).click();
  await expect(form.getByRole("status")).toContainText("Recomputed. saved: wacc");
  await expect(page.getByText("WACC", { exact: true }).locator("..")).toContainText("10.00%");
  await expect(form.locator("input[name=wacc]")).toHaveValue("10");

  await form.getByRole("button", { name: "Clear overrides" }).click();
  await expect(form.getByRole("status")).toContainText("Overrides cleared");
  await expect(form.locator("input[name=wacc]")).toHaveValue("");
  await expect(page.getByText("WACC", { exact: true }).locator("..")).not.toContainText("10.00%");
});

test("a bank has no DCF sensitivity (rule 10)", async ({ page }) => {
  await login(page, "/stocks/DEMOBANK");
  await page.getByRole("tab", { name: "Fair Value", exact: true }).click();
  await expect(page.getByText(/bank model/)).toBeVisible();
  await expect(page.getByText("Justified P/B sensitivity (₹ per share)")).toBeVisible();
});
