// v1 acceptance (P25): search a company by name, open it, watch the on-demand pipeline finish,
// and check the report is complete: Baseline / FV / Top band, zone, grade, action, a coverage
// grid with ≥10 years of P&L, and the data-sources panel. Runs once per golden stock.
//
// Skipped unless E2E_ACCEPTANCE is set:
//   E2E_ACCEPTANCE=live     real data (NSE filings, broker or bhavcopy prices): run on a machine
//                           that can reach NSE, after the nightly symbol master has run.
//                           Stocks: E2E_ACCEPTANCE_STOCKS="hdfc bank=HDFCBANK;..." or the
//                           default five below.
//   E2E_ACCEPTANCE=offline  the synthetic offline exchange (no network), for CI / development:
//                           python -m app.devtools.offline_exchange seed
//                           OFFLINE_EXCHANGE=1 python -m app.jobs pipeline-worker
// See README "Acceptance test" (make acceptance / make acceptance-offline).
import { expect, test, type Page } from "@playwright/test";

import { login } from "./helpers";

type Golden = { query: string; symbol: string };

const MODE = process.env.E2E_ACCEPTANCE;

// One per model type: private bank, IT services, FMCG, cement, NBFC.
const LIVE_DEFAULT =
  "hdfc bank=HDFCBANK;tata consultancy=TCS;hindustan unilever=HINDUNILVR;" +
  "ultratech cement=ULTRACEMCO;bajaj finance=BAJFINANCE";

// The offline exchange's five companies (app/devtools/offline_exchange.py), found by name.
const OFFLINE =
  "offline housing bank=OFFBANK;offline infotech=OFFIT;offline motors=OFFAUTO;" +
  "offline consumer=OFFFMCG;offline cement=OFFCEM";

function parse(spec: string): Golden[] {
  return spec
    .split(";")
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => {
      const [query, symbol] = s.split("=").map((x) => x.trim());
      return { query, symbol: symbol.toUpperCase() };
    });
}

const STOCKS: Golden[] =
  MODE === "offline"
    ? parse(OFFLINE)
    : parse(process.env.E2E_ACCEPTANCE_STOCKS ?? LIVE_DEFAULT);

const MIN_PL_YEARS = 10;

/** Type the company name in the header search, pick the stock, land on its page. */
async function searchAndOpen(page: Page, g: Golden) {
  const box = page.getByRole("combobox", { name: "Search stocks" });
  await box.fill(g.query);
  const option = page
    .getByRole("listbox", { name: "Stocks" })
    .getByRole("option")
    .filter({ hasText: g.symbol })
    .first();
  await expect(option).toBeVisible();
  await option.click();
  await expect(page).toHaveURL(new RegExp(`/stocks/${g.symbol}$`));
}

test.describe("v1 acceptance", () => {
  test.skip(
    MODE !== "live" && MODE !== "offline",
    "set E2E_ACCEPTANCE=live or offline",
  );

  for (const g of STOCKS) {
    test(`"${g.query}" → ${g.symbol}: full report`, async ({ page }) => {
      // first sight of a stock: 10+ years of filings to fetch and parse
      test.setTimeout(MODE === "live" ? 900_000 : 300_000);
      await login(page, "/watchlist");

      // the stock page asks for a pipeline run as it opens (SPEC §3.7)
      const started = page.waitForResponse(
        (r) =>
          r.url().endsWith("/api/pipeline") && r.request().method() === "POST",
      );
      await searchAndOpen(page, g);
      const start = (await (await started).json()) as { run: unknown | null };
      if (start.run === null) {
        // the stored report is fresh: run the pipeline anyway, so it is exercised end to end
        await page.getByRole("button", { name: "Refresh data" }).click();
      }

      // watch it complete
      const panel = page.getByRole("region", {
        name: `Pipeline for ${g.symbol}`,
      });
      await expect(panel).toBeVisible();
      await expect(panel.getByRole("progressbar")).toBeVisible();
      await expect(panel).toContainText("Report updated", {
        timeout: MODE === "live" ? 840_000 : 240_000,
      });
      await expect(panel).not.toContainText("Failed");

      // header: grade, action, zone
      await expect(page.getByRole("heading", { name: g.symbol })).toBeVisible();
      const grade = page.getByLabel(/^Grade /);
      await expect(grade).toBeVisible();
      await expect(grade).not.toHaveAttribute("aria-label", "Grade n/a");
      const header = page.locator("header").filter({ has: grade });
      await expect(header).not.toContainText("No action");
      await expect(header).toContainText(
        /Deep Discount|Discount|Fair|Premium|Extreme Premium/,
      );

      // valuation levels on the zone gauge
      const gauge = page.getByRole("figure", { name: "Valuation zone gauge" });
      for (const level of ["Baseline", "FV", "Top band"]) {
        await expect(
          gauge.getByText(new RegExp(`^${level} ₹[\\d,.]+$`)),
        ).toBeVisible();
      }

      // coverage grid: ≥10 fiscal years of P&L on the basis the report uses
      const tables = page.getByRole("table", { name: /^Data coverage, / });
      await expect(tables.first()).toBeVisible();
      let best = 0;
      for (const t of await tables.all()) {
        const filled = t.locator(
          '[aria-label^="P&L FY"]:not([aria-label$=": gap"])',
        );
        best = Math.max(best, await filled.count());
      }
      expect(best, "years of P&L in the coverage grid").toBeGreaterThanOrEqual(
        MIN_PL_YEARS,
      );

      // sources panel: where prices, fundamentals and shareholding came from
      const sources = page.getByLabel("Data sources", { exact: true });
      await expect(sources).toBeVisible();
      for (const name of ["prices", "fundamentals"]) {
        await expect(sources.locator(`[data-source="${name}"]`)).not.toHaveText(
          "—",
        );
      }
      await expect(sources).toContainText("Shareholding");
      if (MODE === "offline") {
        await expect(
          sources.locator('[data-source="fundamentals"]'),
        ).toHaveText("Offline exchange XBRL (synthetic)");
        await expect(page.getByText("not real market data")).toBeVisible();
      } else {
        await expect(
          sources.locator('[data-source="fundamentals"]'),
        ).toHaveText("Exchange results filings (XBRL)");
      }
    });
  }
});
