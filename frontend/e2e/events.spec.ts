// Corporate events and reconciliation (SPEC v0.2 §3.8-3.9) on the stock page, on the demo seed
// (`python -m app.devtools.demo`): DEMOSOFT has one open cross-source difference, DEMOIT has
// an upcoming results board meeting and recent events.
import { expect, test } from "@playwright/test";

import { login } from "./helpers";

type Issue = { id: number; status: string };

test("reconciliation banner: shows the difference and its cause, ignore hides it", async ({
  page,
}) => {
  await login(page, "/stocks/DEMOSOFT");
  // re-runnable: reopen the demo issue if an earlier run ignored it
  const state = await page.request.get("/api/stocks/DEMOSOFT/reconciliation");
  const body = (await state.json()) as { open: Issue[]; closed: Issue[] };
  for (const i of body.closed.filter((c) => c.status === "ignored")) {
    await page.request.post(
      `/api/stocks/DEMOSOFT/reconciliation/${i.id}/reopen`,
    );
  }
  await page.reload();

  const banner = page.getByRole("alert", { name: "Reconciliation issues" });
  await expect(banner).toContainText(
    "Sources disagree on 1 figure: valuation confidence lowered",
  );
  await expect(banner).toContainText(
    "yfinance ₹820.00 cr vs NSE XBRL ₹1,000.00 cr, 18.0% apart",
  );
  await expect(banner).toContainText(
    "Likely cause: consolidated / standalone mix-up",
  );

  await banner.getByRole("button", { name: "Ignore" }).click();
  await expect(banner).toHaveCount(0);
  const after = await (
    await page.request.get("/api/stocks/DEMOSOFT/reconciliation")
  ).json();
  expect(after.open).toHaveLength(0);
});

test("events card: upcoming board meeting and recent events", async ({
  page,
}) => {
  await login(page, "/stocks/DEMOIT");
  await expect(
    page.getByRole("alert", { name: "Reconciliation issues" }),
  ).toHaveCount(0);
  const upcoming = page.getByRole("list", { name: "Upcoming board meetings" });
  await expect(upcoming).toContainText("Board meeting: Financial Results");
  const events = page.getByRole("list", { name: "Corporate events" });
  await expect(events).toContainText(
    "Bulk deal: DEMO FUND LLP bought 650,000 shares",
  );
  await expect(events).toContainText("Record date for final dividend");
});
