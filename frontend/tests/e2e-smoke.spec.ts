import { expect, test } from "@playwright/test";

test("new task to detail transcript and diff", async ({ page }) => {
  await page.goto("http://localhost:3000");
  await expect(page.getByRole("button", { name: "[ NEW TASK ]" })).toBeVisible();
  await page.getByRole("button", { name: /NEW TASK/ }).click();
  await page.getByLabel("Task description").fill("write hello from frontend smoke");
  await page.getByRole("button", { name: /RUN/ }).click();
  await page.waitForURL(/\/issues\/issue-/, { timeout: 15000 });
  await expect(page.getByText("[DONE]")).toBeVisible({ timeout: 10000 });
  await expect(page.getByText(/REALTIME TRANSCRIPT/)).toBeVisible();
  await expect(page.getByText(/ISSUE_CREATED/)).toBeVisible({ timeout: 10000 });
  await expect(page.getByText(/TASKRUN_QUEUED/)).toBeVisible({ timeout: 10000 });
  await expect(page.getByText(/DIFF \/ CHANGED FILES/)).toBeVisible();
  await page.waitForTimeout(700);
  await page.screenshot({ path: "/tmp/ariadne-detail.png", fullPage: true });
});
