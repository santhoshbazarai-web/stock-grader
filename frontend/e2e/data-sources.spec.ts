// Settings → Data sources (nse-diagnose / bse-diagnose results) and the Results filings
// anchor the "NSE is blocking" notices link to. Does not press Re-check (that would reach out
// to NSE and BSE from the test stack).
import { expect, test } from "@playwright/test";

import { login } from "./helpers";

test("data sources card and upload anchors", async ({ page }) => {
  await login(page, "/settings");
  const card = page.locator("#data-sources");
  await expect(card.getByText("Data sources", { exact: true })).toBeVisible();
  await expect(
    card.getByRole("button", { name: "Re-check now" }),
  ).toBeVisible();
  const res = await page.request.get("/api/data-sources");
  const body = (await res.json()) as {
    nse: { working_method: string | null } | null;
  };
  if (body.nse === null) {
    await expect(card).toContainText("NSE: not checked yet");
    await expect(card).toContainText("python -m app.jobs nse-diagnose");
  } else {
    await expect(
      card.getByRole("table", { name: "NSE endpoints" }),
    ).toBeVisible();
    if (body.nse.working_method === null)
      await expect(card.getByTestId("blocked-notice").first()).toContainText(
        "NSE is blocking automated access from this connection",
      );
  }
  // the targets of the notices' links
  await expect(page.locator("#results-filings")).toBeVisible();
  await expect(page.locator("#screener-uploads")).toBeVisible();
});
