// Manual portfolio on the demo data (python -m app.devtools.demo): create, add, import, edit,
// positions with our grade / zone, charts, CSV export link (+ a screenshot per tab).
import { expect, test, type Page } from "@playwright/test";

import { login } from "./helpers";

const unstick = (page: Page) => page.addStyleTag({ content: "nav[aria-label=Main]{position:static !important}" });
const SHOT = (name: string) => ({ path: `../docs/screenshots/${name}.png`, fullPage: true });

test("portfolio: transactions, positions, dashboard, CSV (+ screenshots)", async ({ page }) => {
  const name = `e2e ${Date.now()}`;
  await login(page, "/portfolio");
  await page.getByLabel("Portfolio name").fill(name);
  await page.getByLabel("Opening cash").fill("200000");
  await page.getByRole("button", { name: "Create", exact: true }).click();
  await expect(page.getByRole("group", { name: "Portfolios" })).toContainText(name);
  await expect(page.getByRole("region", { name: "Summary" })).toContainText("Cash");

  await page.getByRole("tab", { name: "Transactions" }).click();
  await page.getByLabel("Symbol").fill("demotech");
  await page.getByLabel("Quantity", { exact: true }).fill("10");
  await page.getByLabel("Price per share").fill("300");
  await page.getByLabel("Fees").fill("20");
  await page.getByRole("button", { name: "Add transaction" }).click();
  await expect(page.getByText("Transaction added")).toBeVisible();
  await page.getByText("Import / export CSV").click();
  await page.getByLabel("CSV text").fill("date,symbol,type,quantity,price,fees,notes\n2024-02-01,DEMOFMCG,buy,5,1000,10,csv\n2024-03-01,DEMOFMCG,sell,2,1100,5,\n2024-03-02,DEMOFMCG,hold,1,1,0,\n");
  await page.getByRole("button", { name: "Import", exact: true }).click();
  await expect(page.getByText(/^Imported 2/)).toBeVisible();
  await expect(page.getByText(/line 4/)).toBeVisible();
  await expect(page.getByRole("link", { name: "Export CSV" })).toHaveAttribute("href", /transactions\/export$/);
  const table = page.getByRole("table", { name: "Transactions" });
  await expect(table.locator("tbody tr")).toHaveCount(3);
  await unstick(page);
  await page.screenshot(SHOT("portfolio-transactions"));

  await page.getByRole("button", { name: /Edit transaction/ }).first().click();
  await page.getByLabel("Fees").fill("25");
  await page.getByRole("button", { name: "Save changes" }).click();
  await expect(page.getByText("Transaction updated")).toBeVisible();

  await page.getByRole("tab", { name: "Positions" }).click();
  const pos = page.getByRole("table", { name: "Positions" });
  await expect(pos).toContainText("DEMOTECH");
  await expect(pos).toContainText("DEMOFMCG");
  await expect(pos).toContainText("₹");
  await unstick(page);
  await page.screenshot(SHOT("portfolio-positions"));

  await page.getByRole("tab", { name: "Dashboard" }).click();
  await expect(page.getByRole("figure", { name: "Allocation by sector" })).toBeVisible();
  await expect(page.getByRole("figure", { name: "Unrealised P&L by holding" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Summary" })).toContainText("XIRR");
  await page.waitForTimeout(800);
  await unstick(page);
  await page.screenshot(SHOT("portfolio-dashboard"));

  page.once("dialog", (d) => d.accept());
  await page.getByRole("button", { name: "Delete portfolio" }).click();
  await expect(page.getByRole("group", { name: "Portfolios" })).not.toContainText(name);
});
