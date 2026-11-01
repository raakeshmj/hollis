import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { OrchestrationView, RunDetail as Detail, RunSummary } from "../api/types";
import { RunDetail } from "../pages/RunDetail";
import { admin, renderAt, stubApi } from "../test/render";

const root = "aaaaaaaa-0000-4000-8000-000000000000";
const research = "bbbbbbbb-0000-4000-8000-000000000000";
const review = "cccccccc-0000-4000-8000-000000000000";
const t0 = "2026-11-01T10:00:00Z";

function summary(over: Partial<RunSummary>): RunSummary {
  return {
    run_id: root, phase: "completed", status: "completed", agent: "lead", created_by: "admin", session: null,
    model: "demo", provider: "hollis", protocol: "hollis-demo", input_preview: "team task", created_at: t0,
    updated_at: "2026-11-01T10:00:04Z", awaiting_approvals: 0, turns: 2, tool_calls: 2,
    usage: { input_tokens: 300, output_tokens: 40 }, error: null, duration_s: 4, parent_run_id: null,
    root_run_id: null, depth: 0, children: 2, objective: null, ...over,
  };
}

const tree: OrchestrationView = {
  root_run_id: root,
  focus_run_id: root,
  limits: { max_depth: 2, max_children: 8, max_runs: 24, token_budget: 50000, wait_s: 900 },
  nodes: [
    { run: summary({}), inputs: [], narrowed: [], started_at: t0, finished_at: "2026-11-01T10:00:04Z" },
    {
      run: summary({ run_id: research, agent: "researcher", depth: 1, parent_run_id: root, root_run_id: root,
        children: 0, objective: "how do we deploy?", duration_s: 1 }),
      inputs: [], narrowed: ["knowledge finance"], started_at: "2026-11-01T10:00:01Z",
      finished_at: "2026-11-01T10:00:02Z",
      report: { outcome: "completed", status: "done", confidence: "low", answer: "make ship", evidence: ["C-0000beef"],
        open_questions: [], side_effects_unknown: [],
        verification: { claims: 2, supported: 1, weak: 0, uncited: 1, fabricated: 0 } },
    },
    {
      run: summary({ run_id: review, agent: "reviewer", depth: 1, parent_run_id: root, root_run_id: root,
        children: 0, objective: "check it", phase: "failed", duration_s: 1 }),
      inputs: [research], narrowed: [], started_at: "2026-11-01T10:00:02Z", finished_at: "2026-11-01T10:00:03Z",
      report: { outcome: "failed", reason: "provider_error: invalid key", evidence: [], open_questions: [],
        side_effects_unknown: ["fs_write_file"] },
    },
  ],
  totals: { runs: 3, phases: { completed: 2, failed: 1 }, input_tokens: 900, output_tokens: 120,
    coordinator_tokens: 340, worker_tokens: 680, wall_s: 4, agent_s: 2, max_depth: 1, waiting_approvals: 0, failed: 1 },
  refusals: [{ parent_run_id: root, agent: "writer", reason: "this run already delegated 2 tasks (limit 2)" }],
  disagreements: [{ objective: "refund window?", runs: [research, review], answers: ["14 days", "30 days"] }],
  hidden: 0,
};

const detail = {
  summary: summary({}), input: "@researcher how do we deploy? then @reviewer check it", instructions: null,
  budget: { max_output_tokens: 1024 }, context_policy: {}, tools: ["delegate_researcher"], memory_access: null,
  knowledge_access: null, memories: [], passages: [], grounding: null, compactions: [], turns: [], answer: "done",
  failure: null, cancellation: null, deadline: null, lineage: [], assignment: null, delegation: tree.limits,
} as unknown as Detail;

describe("Orchestration inspector", () => {
  it("shows the tree, dependencies, reports, timeline, refusals and disagreements", async () => {
    stubApi({
      "GET /v1/session": [200, { principal: admin, expires_at: null }],
      [`GET /v1/runs/${root}`]: [200, detail],
      [`GET /v1/runs/${root}/tree`]: [200, tree],
      "GET /v1/approvals": [200, []],
    });
    renderAt(<RunDetail />, { route: `/runs/${root}`, path: "/runs/:id" });
    const list = await screen.findByRole("list", { name: "Runs in this orchestration" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    const [, first, second] = items as [HTMLElement, HTMLElement, HTMLElement];
    expect(within(first).getByRole("link", { name: "researcher" })).toHaveAttribute("href", `/runs/${research}`);
    expect(within(first).getByText("how do we deploy?")).toBeInTheDocument();
    expect(within(first).getByText("1/2 supported")).toBeInTheDocument();
    expect(within(first).getByText("not granted: knowledge finance")).toBeInTheDocument();
    expect(within(first).getByText(/self-reported: done, low confidence/)).toBeInTheDocument();
    expect(within(second).getByRole("link", { name: "researcher" })).toHaveClass("tag");
    expect(within(second).getByText(/failed: provider_error: invalid key/)).toBeInTheDocument();
    expect(within(second).getByText(/Unknown side effects .*fs_write_file/)).toBeInTheDocument();
    expect(screen.getByText(/coordination 340, work 680/)).toBeInTheDocument();
    expect(screen.getByText(/50.0k token budget/)).toBeInTheDocument();
    const timeline = screen.getByRole("list", { name: "When each run ran" });
    expect(within(timeline).getByText(/researcher: started 1.0 s in, took 1.0 s/)).toBeInTheDocument();
    expect(screen.getByText(/this run already delegated 2 tasks/)).toBeInTheDocument();
    expect(screen.getByText(/Kept as reported, not resolved/)).toBeInTheDocument();
    expect(screen.getByText(/“30 days”/)).toBeInTheDocument();
  });

  it("shows a delegated run's lineage and assignment", async () => {
    const child = {
      ...detail,
      summary: summary({ run_id: research, agent: "researcher", depth: 1, parent_run_id: root, root_run_id: root,
        children: 0, objective: "how do we deploy?" }),
      lineage: [{ run_id: root, agent: "lead", objective: null }],
      assignment: { agent: "researcher", objective: "how do we deploy?", context: "", expected_output: "a command",
        inputs: [], evidence: [] },
    } as unknown as Detail;
    stubApi({
      "GET /v1/session": [200, { principal: admin, expires_at: null }],
      [`GET /v1/runs/${research}`]: [200, child],
      [`GET /v1/runs/${research}/tree`]: [200, { ...tree, focus_run_id: research }],
      "GET /v1/approvals": [200, []],
    });
    renderAt(<RunDetail />, { route: `/runs/${research}`, path: "/runs/:id" });
    const crumbs = await screen.findByRole("navigation", { name: "Delegated by" });
    expect(within(crumbs).getByRole("link", { name: /lead/ })).toHaveAttribute("href", `/runs/${root}`);
    const assignment = screen.getByRole("region", { name: "Assignment" });
    expect(within(assignment).getByText("a command")).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Open the root run" })).toHaveAttribute("href", `/runs/${root}`);
  });
});
