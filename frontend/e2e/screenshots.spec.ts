// UI screenshots of the HDFCBANK stock page for the PR (desktop / mobile × light / dark).
//   SHOTS=1 E2E_PASSWORD=<APP_PASSWORD> npx playwright test e2e/screenshots.spec.ts
// Output: docs/screenshots/*.png. Skipped unless SHOTS=1 (it needs a seeded stack).
import { expect, test } from "@playwright/test";

import { login } from "./helpers";

const SIZES = { desktop: { width: 1360, height: 900 }, mobile: { width: 390, height: 844 } } as const;
const SCHEMES = ["light", "dark"] as const;
const OUT = "../docs/screenshots";

test.skip(!process.env.SHOTS, "set SHOTS=1 to take screenshots");

for (const [device, viewport] of Object.entries(SIZES)) {
  for (const scheme of SCHEMES) {
    test(`HDFCBANK ${device} ${scheme}`, async ({ browser }) => {
      const ctx = await browser.newContext({ viewport, colorScheme: scheme, deviceScaleFactor: device === "mobile" ? 2 : 1 });
      const page = await ctx.newPage();
      await login(page, "/stocks/HDFCBANK");
      await expect(page.getByRole("heading", { name: /HDFC/ }).first()).toBeVisible();
      await expect(page.getByTestId("overview-tables")).toBeVisible();
      await expect(page.getByTestId("price-chart")).toBeVisible();
      await page.waitForTimeout(1500); // chart + async cards settle
      await page.screenshot({ path: `${OUT}/hdfcbank-${device}-${scheme}.png`, fullPage: true });
      if (device === "desktop") {
        await page.getByRole("tab", { name: "Fair Value" }).click();
        await page.waitForTimeout(800);
        await page.screenshot({ path: `${OUT}/hdfcbank-fair-value-${scheme}.png`, fullPage: true });
      }
      await ctx.close();
    });
  }
}
