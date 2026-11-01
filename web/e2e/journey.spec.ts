import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { STATE } from "./global-setup";

const { token } = JSON.parse(readFileSync(STATE, "utf8")) as { token: string };

const nav = (page: Page, name: string) =>
  page.getByRole("navigation", { name: "Sections" }).getByRole("link", { name, exact: true });

async function signIn(page: Page) {
  await page.goto(`/login#token=${token}`);
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
}

let errors: string[] = [];

test.beforeEach(async ({ page }) => {
  errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
});

test.afterEach(() => {
  expect(errors, "uncaught errors in the page").toEqual([]);
});

test("a run that needs approval, decided in the browser, finishes live", async ({ page }) => {
  await signIn(page);
  await expect(page.getByText("Runs use the built-in demo model.")).toBeVisible();
  await page.getByLabel("Task for the agent").fill("write e2e.txt: written from the browser");
  await page.getByRole("button", { name: "Start run" }).click();

  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);
  await expect(page.getByText("waiting approval").first()).toBeVisible();
  const approval = page.getByRole("article", { name: "fs_write_file waiting for approval" });
  await expect(approval.getByText("e2e.txt")).toBeVisible();
  await approval.getByPlaceholder(/Reason/).fill("approved in e2e");
  await approval.getByRole("button", { name: "Approve" }).click();

  await expect(page.getByText("completed").first()).toBeVisible();
  await expect(page.getByText(/approved by admin/)).toBeVisible();
  await expect(page.locator(".answer")).toContainText("[demo]");

  await nav(page, "Approvals").click();
  await expect(page.getByText("Nothing is waiting")).toBeVisible();
});

test("documents are uploaded, searched and used with citations", async ({ page }) => {
  await signIn(page);
  await nav(page, "Knowledge").click();
  await page.getByRole("link", { name: "docs", exact: true }).click();
  await page.getByLabel("Files").setInputFiles({
    name: "runbook.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("# Runbook\n\nRestart the worker with `make restart` after a deploy.\n"),
  });
  await expect(page.getByRole("button", { name: "runbook.md" })).toBeVisible();

  await nav(page, "Knowledge").click();
  await page.getByLabel("Search documents").fill("restart the worker");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page.locator(".passage").first()).toContainText("make restart");
});

test("memory, tokens and the theme", async ({ page }) => {
  await signIn(page);
  await nav(page, "Memory").click();
  await page.getByLabel("Content", { exact: true }).fill("Deploys happen on Tuesdays.");
  await page.getByRole("combobox", { name: "Scope", exact: true }).fill("workspace:main");
  await page.getByRole("button", { name: "Remember" }).click();
  await expect(page.getByRole("button", { name: "Deploys happen on Tuesdays." })).toBeVisible();

  await nav(page, "Settings").click();
  await page.getByLabel("Label", { exact: true }).fill("from e2e");
  await page.getByRole("button", { name: "Create token" }).click();
  await expect(page.getByText("Copy this token now.")).toBeVisible();
  await expect(page.locator(".secret code")).toContainText("hx_");

  const html = page.locator("html");
  await page.getByRole("button", { name: /Change theme/ }).click();
  await expect(html).toHaveAttribute("data-theme", "light");
  await page.getByRole("button", { name: /Change theme/ }).click();
  await expect(html).toHaveAttribute("data-theme", "dark");
});

test("every page has a heading and keyboard-reachable navigation", async ({ page }) => {
  await signIn(page);
  for (const name of ["Runs", "Approvals", "Agents", "Tools", "Models", "Knowledge", "Memory", "Settings"]) {
    await nav(page, name).click();
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  }
  await page.reload();
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
});

test("signing out ends the session", async ({ page }) => {
  await signIn(page);
  const checks: number[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/v1/session") && request.method() === "GET") checks.push(Date.now());
  });
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  await page.goto("/runs");
  await expect(page).toHaveURL(/\/login\?next=%2Fruns/);
  await page.waitForTimeout(1000);
  expect(checks.length).toBeLessThan(6); // no retry loop once signed out
});
