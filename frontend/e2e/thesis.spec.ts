// AI thesis card (SPEC §8a). Without GEMINI_API_KEY the card says "Add GEMINI_API_KEY to
// .env". With a key, or THESIS_LLM_URL=fake on the API (a built-in stand-in), the card writes
// the thesis, shows the checked text with the disclaimer and offers Regenerate.
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
    await expect(card).toContainText("Add GEMINI_API_KEY to .env");
    await expect(card.getByRole("button")).toHaveCount(0);
    return;
  }

  const button = card.getByRole("button", {
    name: /Write thesis|Regenerate|Try again/,
  });
  await button.click();
  const text = card.getByTestId("thesis-text");
  await expect(text).toBeVisible({ timeout: 60_000 });
  await expect(text).toContainText("DEMOIT");
  await expect(card).toContainText("Every number in it was checked");
  await expect(text).toContainText("Not investment advice.");
  await expect(card.getByRole("button", { name: "Regenerate" })).toBeVisible();
  // the stored report carries it too
  const report = await page.request.get("/api/stocks/DEMOIT/report");
  const stored = ((await report.json()) as { thesis: string | null }).thesis ?? "";
  expect(stored.replace(/\s+/g, " ")).toBe(((await text.textContent()) ?? "").replace(/\s+/g, " "));
});
