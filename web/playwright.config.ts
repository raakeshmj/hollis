import { defineConfig } from "@playwright/test";

// Opt-in browser tests against a real `hollis serve` (see e2e/global-setup.ts). They use the
// installed Google Chrome, so nothing is downloaded: `npm run test:e2e` or `make web-e2e`.
export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  workers: 1,
  reporter: [["list"]],
  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",
  use: {
    channel: "chrome",
    baseURL: process.env.HOLLIS_E2E_URL ?? "http://127.0.0.1:7895",
    trace: "retain-on-failure",
  },
});
