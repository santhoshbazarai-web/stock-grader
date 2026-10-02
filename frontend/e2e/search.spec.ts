// Header search (SPEC §3.5) on the demo symbol master (python -m app.devtools.demo): symbol,
// BSE code, former name, keyboard navigation, and a BSE-only company that has no stock page.
import { expect, test } from "@playwright/test";

import { login } from "./helpers";

test("header search: codes, former names, keyboard navigation", async ({
  page,
}) => {
  await login(page, "/watchlist");
  const box = page.getByRole("combobox", { name: "Search stocks" });
  const list = page.getByRole("listbox", { name: "Stocks" });

  // "/" focuses the box from anywhere
  await page.locator("body").click();
  await page.keyboard.press("/");
  await expect(box).toBeFocused();

  // a BSE code resolves to its NSE stock
  await box.fill("990001");
  await expect(list.getByRole("option").first()).toContainText("DEMOIT");
  await expect(list.getByRole("option").first()).toContainText("BSE 990001");

  // an old company name, marked as such
  await box.fill("demo infotech systems");
  await expect(list.getByRole("option").first()).toContainText(
    "formerly Demo Infotech Systems Ltd",
  );
  await box.press("Enter");
  await expect(page).toHaveURL(/\/stocks\/DEMOIT$/);
  await expect(box).toHaveValue("");

  // keyboard: ↓ moves the highlight (aria-activedescendant), Enter opens it
  await page.mouse.move(0, 0); // a resting pointer over a result would hover-select it
  await box.fill("demo");
  const options = list.getByRole("option");
  await expect(options.nth(2)).toBeVisible();
  await expect(options.first()).toHaveAttribute("aria-selected", "true");
  await box.press("ArrowDown");
  await box.press("ArrowDown");
  await expect(options.nth(2)).toHaveAttribute("aria-selected", "true");
  const third = (
    await options.nth(2).locator("span.font-medium").textContent()
  )?.trim();
  await expect(box).toHaveAttribute(
    "aria-activedescendant",
    (await options.nth(2).getAttribute("id")) ?? "",
  );
  await box.press("ArrowUp");
  await box.press("ArrowDown");
  await box.press("Enter");
  await expect(page).toHaveURL(new RegExp(`/stocks/${third}$`));

  // Esc closes the list, a second Esc clears the box
  await box.fill("demo rural");
  await expect(list).toBeVisible();
  const bseOnly = list.getByRole("option").first();
  await expect(bseOnly).toContainText("BSE only");
  await expect(bseOnly).toHaveAttribute("aria-disabled", "true");
  await box.press("Escape");
  await expect(list).toBeHidden();
  await expect(box).toHaveValue("demo rural");
  await box.press("Escape");
  await expect(box).toHaveValue("");

  // Ctrl+K focuses it too
  await page.locator("body").click();
  await page.keyboard.press("Control+k");
  await expect(box).toBeFocused();
});
