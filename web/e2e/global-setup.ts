/** Starts `hollis serve` on a fresh workspace with the demo model, and mints an API token. */
import { execFileSync, spawn } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const project = resolve(import.meta.dirname, "../..");
export const STATE = join(tmpdir(), "hollis-e2e-state.json");

function hollis(args: string[], cwd: string): string {
  return execFileSync("uv", ["run", "--project", project, "hollis", ...args], { cwd, encoding: "utf8" });
}

export default async function setup() {
  const workspace = mkdtempSync(join(tmpdir(), "hollis-e2e-"));
  hollis(["init", "--name", "e2e"], workspace);
  const server = spawn("uv", ["run", "--project", project, "hollis", "serve", "--port", "7895"], {
    cwd: workspace,
    stdio: "ignore",
    detached: true,
  });
  server.unref();
  for (let i = 0; i < 120; i++) {
    try {
      const response = await fetch("http://127.0.0.1:7895/v1/ping");
      if (response.ok) break;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  const token = JSON.parse(hollis(["--json", "tokens", "create", "--label", "e2e"], workspace)).token as string;
  writeFileSync(STATE, JSON.stringify({ workspace, pid: server.pid, token }));
}
