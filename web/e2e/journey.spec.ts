import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { STATE } from "./global-setup";

const { token } = JSON.parse(readFileSync(STATE, "utf8")) as { token: string };

const nav = (page: Page, name: string) =>
  page
    .getByRole("navigation", { name: "Sections" })
    .getByRole("link", { name, exact: true });

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

test("a run that needs approval, decided in the browser, finishes live", async ({
  page,
}) => {
  await signIn(page);
  await expect(
    page.getByText("Runs use the built-in demo model."),
  ).toBeVisible();
  await page
    .getByLabel("Task for the agent")
    .fill("write e2e.txt: written from the browser");
  await page.getByRole("button", { name: "Start run" }).click();

  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);
  await expect(page.getByText("waiting approval").first()).toBeVisible();
  const approval = page.getByRole("article", {
    name: "fs_write_file waiting for approval",
  });
  await expect(approval.getByText("e2e.txt")).toBeVisible();
  await approval.getByPlaceholder(/Reason/).fill("approved in e2e");
  await approval.getByRole("button", { name: "Approve" }).click();

  await expect(page.getByText("completed").first()).toBeVisible();
  await expect(page.getByText(/approved by admin/)).toBeVisible();
  await expect(page.locator(".answer")).toContainText("[demo]");

  await nav(page, "Approvals").click();
  await expect(page.getByText("Nothing is waiting")).toBeVisible();
});

test("documents are uploaded, searched and used with citations", async ({
  page,
}) => {
  await signIn(page);
  await nav(page, "Knowledge").click();
  await page.getByRole("link", { name: "docs", exact: true }).click();
  await page.getByLabel("Files").setInputFiles({
    name: "runbook.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(
      "# Runbook\n\nRestart the worker with `make restart` after a deploy.\n",
    ),
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
  await page
    .getByLabel("Content", { exact: true })
    .fill("Deploys happen on Tuesdays.");
  await page
    .getByRole("combobox", { name: "Scope", exact: true })
    .fill("workspace:main");
  await page.getByRole("button", { name: "Remember" }).click();
  await expect(
    page.getByRole("button", { name: "Deploys happen on Tuesdays." }),
  ).toBeVisible();

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

test("every page has a heading and keyboard-reachable navigation", async ({
  page,
}) => {
  await signIn(page);
  for (const name of [
    "Runs",
    "Approvals",
    "Agents",
    "Templates",
    "Tools",
    "Models",
    "Knowledge",
    "Memory",
    "Settings",
  ]) {
    await nav(page, name).click();
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  }
  await page.reload();
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("link", { name: "Skip to content" }),
  ).toBeFocused();
});

test("signing out ends the session", async ({ page }) => {
  await signIn(page);
  const checks: number[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/v1/session") && request.method() === "GET")
      checks.push(Date.now());
  });
  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible();
  await page.goto("/runs");
  await expect(page).toHaveURL(/\/login\?next=%2Fruns/);
  await page.waitForTimeout(1000);
  expect(checks.length).toBeLessThan(6); // no retry loop once signed out
});

test("an orchestration is inspected from the root and from a delegated run", async ({
  page,
}) => {
  const api = (path: string, body: object) =>
    page.request.post(`/v1${path}`, {
      data: body,
      headers: { authorization: `Bearer ${token}` },
    });
  expect(
    (
      await api("/agents", {
        name: "researcher",
        model: "demo",
        description: "finds things",
      })
    ).ok(),
  ).toBe(true);
  expect(
    (
      await api("/agents", {
        name: "lead",
        model: "demo",
        delegates: ["researcher"],
      })
    ).ok(),
  ).toBe(true);
  const started = await api("/runs", {
    input: "@researcher what is hollis?",
    agent: "lead",
  });
  const { run_id: runId } = (await started.json()) as { run_id: string };

  await signIn(page);
  await page.goto(`/runs/${runId}`);
  const section = page.getByRole("region", { name: "Orchestration" });
  await expect(section).toBeVisible();
  await expect(section.getByText("completed").first()).toBeVisible();
  await expect(
    section.getByRole("heading", { name: "Timeline" }),
  ).toBeVisible();
  await expect(section.getByText("what is hollis?").first()).toBeVisible();

  await section.getByRole("link", { name: "researcher" }).first().click();
  await expect(
    page.getByRole("navigation", { name: "Delegated by" }),
  ).toContainText("lead");
  await expect(page.getByRole("region", { name: "Assignment" })).toContainText(
    "what is hollis?",
  );

  await nav(page, "Runs").click();
  await expect(page.getByText("+1 delegated")).toBeVisible();
  await expect(page.getByText("@researcher what is hollis?")).toBeVisible();
});

test("a specialist is made from a template and its checked output is shown", async ({
  page,
}) => {
  await signIn(page);
  await nav(page, "Templates").click();
  const gallery = page.getByRole("list", { name: "Templates" });
  await expect(
    gallery.getByRole("link", { name: "Code reviewer" }),
  ).toBeVisible();
  await gallery.getByRole("link", { name: "Code reviewer" }).click();
  await expect(
    page.getByRole("heading", { name: "Code reviewer", level: 1 }),
  ).toBeVisible();

  await page.getByLabel("Agent name").fill("reviewer-e2e");
  await page.getByRole("button", { name: "Create agent" }).click();
  await expect(page).toHaveURL(/\/agents\/reviewer-e2e$/);
  await expect(page.getByRole("region", { name: "Template" })).toContainText(
    "code-reviewer",
  );

  await page
    .getByLabel("Task for the agent")
    .fill("Review the hollis configuration");
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}$/);
  const output = page.getByRole("region", { name: /Output/ });
  await expect(output).toContainText("Accepted: every reference was checked");
  await expect(output).toContainText("code-review");
});
