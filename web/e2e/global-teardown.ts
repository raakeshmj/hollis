import { readFileSync, rmSync } from "node:fs";
import { STATE } from "./global-setup";

export default async function teardown() {
  const { pid, workspace } = JSON.parse(readFileSync(STATE, "utf8")) as { pid: number; workspace: string };
  try {
    process.kill(-pid, "SIGINT"); // the process group: uv and the server it started
  } catch {
    // already stopped
  }
  await new Promise((r) => setTimeout(r, 1500));
  rmSync(workspace, { recursive: true, force: true });
  rmSync(STATE, { force: true });
}
