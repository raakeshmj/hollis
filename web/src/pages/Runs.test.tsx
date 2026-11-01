import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { admin, renderAt, stubApi } from "../test/render";
import { Runs } from "./Runs";

const run = {
  run_id: "dddddddd-0000-4000-8000-000000000000", phase: "completed", status: "completed", agent: null,
  created_by: "admin", session: null, model: "workflow", provider: "hollis", protocol: "hollis-workflow",
  input_preview: "Brief", created_at: "2026-11-01T10:00:00Z", updated_at: "2026-11-01T10:00:01Z",
  awaiting_approvals: 0, turns: 2, tool_calls: 2, usage: { input_tokens: 0, output_tokens: 0 }, error: null,
  duration_s: 1, parent_run_id: null, root_run_id: null, depth: 0, children: 2, objective: null,
};

describe("Runs", () => {
  it("lists root runs by default and can include delegated ones", async () => {
    const calls = stubApi({
      "GET /v1/session": [200, { principal: admin, expires_at: null }],
      "GET /v1/agents": [200, []],
      "GET /v1/runs": [200, { items: [run], total: 1 }],
    });
    renderAt(<Runs />);
    expect(await screen.findByText("+2 delegated")).toBeInTheDocument();
    expect(screen.getByText("plan")).toBeInTheDocument();
    expect(calls.find((c) => c.url.startsWith("/v1/runs"))?.url).toContain("roots=true");
    await userEvent.click(screen.getByRole("checkbox", { name: "Include delegated runs" }));
    await waitFor(() => {
      const last = calls.filter((c) => c.url.startsWith("/v1/runs")).at(-1);
      expect(last?.url).not.toContain("roots");
    });
  });
});
