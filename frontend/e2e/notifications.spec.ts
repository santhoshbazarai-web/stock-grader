// Notification centre (P23): notifications from every source in one list, filtered by type,
// read/unread toggles, the bell's "View all", and the Telegram card.
import { expect, test } from "@playwright/test";

import { login } from "./helpers";

test("notification centre: filter, toggle read, bell link, Telegram card", async ({ page }) => {
  await login(page, "/notifications");
  // a fresh test notification (created like every other: in-app, plus Telegram if configured)
  const created = await (await page.request.post("/api/notifications/test")).json();
  await page.reload();

  const list = page.getByRole("list", { name: "Notification list" });
  await page.getByRole("button", { name: /^Tests \d+$/ }).click();
  const row = list.getByRole("listitem", { name: "Unread: Stock Grader test notification" }).first();
  await expect(row).toBeVisible();
  await expect(row).toContainText(created.telegram === "sent" ? "sent to Telegram" : "in-app only");

  await row.getByRole("button", { name: "Mark read" }).click();
  await expect(list.getByRole("listitem", { name: "Read: Stock Grader test notification" }).first()).toBeVisible();

  await page.getByRole("button", { name: "Unread", exact: true }).click();
  await expect(page.getByRole("button", { name: "Unread", exact: true })).toHaveAttribute("aria-pressed", "true");

  const telegram = page.getByRole("region", { name: "Telegram" });
  await expect(telegram).toContainText("/grade SYMBOL");
  await expect(telegram.getByRole("status")).not.toBeEmpty();

  await page.goto("/");
  await page.getByRole("button", { name: /^Notifications/ }).click();
  await page.getByRole("dialog", { name: "Notifications" }).getByRole("link", { name: "View all" }).click();
  await expect(page).toHaveURL(/\/notifications$/);
  await expect(page.getByRole("heading", { name: "Notifications" })).toBeVisible();
});
