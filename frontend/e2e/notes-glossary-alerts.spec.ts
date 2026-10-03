// Notes, glossary, alerts and watchlist pages: one flow each, plus a screenshot per page in
// docs/screenshots (needs the demo data: python -m app.devtools.demo).
import { expect, test } from "@playwright/test";

import { login } from "./helpers";

const SHOT = (name: string) => ({ path: `../docs/screenshots/${name}.png`, fullPage: true });
const unstick = (page: import("@playwright/test").Page) => page.addStyleTag({ content: "nav[aria-label=Main]{position:static !important}" });

test("notes: add on the stock page, see it on My Notes, edit, delete (+ screenshot)", async ({ page }) => {
  const text = `e2e note ${Date.now()}`;
  await login(page, "/stocks/DEMOTECH");
  await page.getByRole("tab", { name: "Notes" }).click();
  await page.getByLabel("Note", { exact: true }).fill(`**${text}**\n- cheap\n- watch margins`);
  await page.getByRole("button", { name: "Add note" }).click();
  await expect(page.getByRole("tabpanel").getByText(text)).toBeVisible();

  await page.goto("/notes");
  await expect(page.getByText(text)).toBeVisible();
  await page.getByLabel("Search notes").fill("watch margins");
  await expect(page.getByText(text)).toBeVisible();
  await unstick(page);
  await page.screenshot(SHOT("notes"));
  await page.getByLabel("Search notes").fill("");
  await page.getByRole("button", { name: "Edit" }).first().click();
  await page.getByLabel("Edit note").fill(`edited ${text}`);
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByText(`edited ${text}`)).toBeVisible();
  await page.getByRole("button", { name: /Delete note/ }).first().click();
  await expect(page.getByText(`edited ${text}`)).toHaveCount(0);
});

test("glossary: A-Z index, search, anchors from metric labels (+ screenshot)", async ({ page }) => {
  await login(page, "/glossary");
  await expect(page.getByRole("navigation", { name: "A to Z index" })).toBeVisible();
  await expect(page.locator("#roe")).toContainText("Return on equity");
  await page.getByLabel("Search glossary").fill("dividend");
  await expect(page.locator("#dividend_yield")).toBeVisible();
  await expect(page.locator("#roe")).toHaveCount(0);
  await page.getByLabel("Search glossary").fill("");
  await unstick(page);
  await page.screenshot(SHOT("glossary"));
  // a metric label on the stock page links to its entry
  await page.goto("/stocks/DEMOTECH");
  await page.getByRole("link", { name: "P/E", exact: true }).first().click();
  await expect(page).toHaveURL(/\/glossary#pe$/);
});

test("alerts and watchlist pages (+ screenshots)", async ({ page }) => {
  await login(page, "/stocks/DEMOTECH");
  await page.getByRole("button", { name: "Add alert" }).click();
  await page.getByLabel("Alert type").selectOption("crosses_fv");
  await page.getByRole("button", { name: "Create alert" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Alert set" })).toBeVisible();

  await page.goto("/alerts");
  await expect(page.getByRole("table", { name: "Alerts" })).toContainText("DEMOTECH");
  await unstick(page);
  await page.screenshot(SHOT("alerts"));

  await page.goto("/watchlist");
  await page.getByText("Import from CSV").click();
  await page.getByLabel("CSV text").fill("DEMOTECH\nDEMOFMCG\n");
  await page.getByRole("button", { name: "Import", exact: true }).click();
  await expect(page.getByRole("table", { name: "Watchlist" })).toContainText("DEMOFMCG");
  await page.getByRole("link", { name: "Manage alerts for DEMOTECH" }).click();
  await expect(page).toHaveURL(/\/alerts\?symbol=DEMOTECH/);
  await page.goBack();
  await expect(page.getByRole("table", { name: "Watchlist" })).toContainText("DEMOTECH");
  await expect(page.getByRole("tab").first()).toBeVisible();
  await unstick(page);
  await page.screenshot(SHOT("watchlist"));
  for (const s of ["DEMOTECH", "DEMOFMCG"]) await page.getByRole("button", { name: `Remove ${s}` }).click();
});
