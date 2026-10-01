// LLM thesis card (SPEC §8a). The default stack has the generator off: the card says so. With
// it on (jobs.yaml thesis.enabled: true and THESIS_LLM_URL=fake on the API, a built-in
// stand-in for a local model), the card writes the thesis and shows the checked text.
import { expect, test } from "@playwright/test";

import { login } from "./helpers";

test("thesis card: off message, or write and show a checked thesis", async ({
  page,
}) => {
  await login(page, "/stocks/DEMOIT");
  const res = await page.request.get("/api/stocks/DEMOIT/thesis");
  expect(res.ok()).toBeTruthy();
  const { status } = (await res.json()) as { status: string };
  const card = page
    .locator("[data-slot=card]")
    .filter({ has: page.getByText("Thesis", { exact: true }) });
  await expect(card).toBeVisible();

  if (status === "disabled") {
    await expect(card).toContainText("Off:");
    await expect(card).toContainText("thesis.enabled");
    await expect(card.getByRole("button")).toHaveCount(0);
    return;
  }

  const button = card.getByRole("button", {
    name: /Write thesis|Rewrite|Try again/,
  });
  await button.click();
  const text = card.getByTestId("thesis-text");
  await expect(text).toBeVisible({ timeout: 60_000 });
  await expect(text).toContainText("DEMOIT");
  await expect(card).toContainText("Every number in it was checked");
  await expect(card.getByRole("button", { name: "Rewrite" })).toBeVisible();
  // the stored report carries it too
  const report = await page.request.get("/api/stocks/DEMOIT/report");
  expect(((await report.json()) as { thesis: string | null }).thesis).toBe(
    await text.textContent(),
  );
});
