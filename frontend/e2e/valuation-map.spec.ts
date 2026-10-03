import { expect, test } from "@playwright/test";

import { login } from "./helpers";

test("valuation map: treemap, distribution, chips, footnote (+ screenshot)", async ({ page }) => {
  await login(page, "/valuation-map");
  await expect(page.getByRole("heading", { name: "Valuation Map" })).toBeVisible();
  await expect(page.getByTestId("valuation-treemap").locator("svg")).toBeVisible();
  await expect(page.getByTestId("cheap-share")).toContainText("of the universe is at discount or fair");
  await expect(page.getByLabel("Distribution by zone and grade")).toBeVisible();
  await expect(page.getByLabel("Colour legend")).toBeVisible();
  await expect(page.getByTestId("map-footnote")).toContainText("stocks shown");
  await page.getByLabel("Colour by").selectOption("grade");
  await expect(page.getByLabel("Colour legend")).toContainText("A+");
  await page.getByLabel("Group by").selectOption("industry");
  await page.getByLabel("Group by").selectOption("sector");
  await page.getByRole("button", { name: "Show all" }).click();
  await page.getByLabel("Colour by").selectOption("discount");
  await page.waitForTimeout(500);
  await page.addStyleTag({ content: "nav[aria-label=Main]{position:static !important}" }); // sticky bars float mid-page in full-page shots
  await page.screenshot({ path: "../docs/screenshots/valuation-map.png", fullPage: true });
  // a stock tile opens its page
  await page.getByTestId("valuation-treemap").getByRole("link").first().click();
  await expect(page).toHaveURL(/\/stocks\/[A-Z]+$/);
});
