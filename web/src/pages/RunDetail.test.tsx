import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { RunDetail as Detail } from "../api/types";
import { admin, renderAt, stubApi } from "../test/render";
import { RunDetail } from "./RunDetail";

const id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const now = "2026-11-01T10:00:00Z";
const detail = {
  summary: {
    run_id: id, phase: "completed", status: "completed", agent: "helper", created_by: "admin", session: null,
    model: "demo", provider: "hollis", protocol: "hollis-demo", input_preview: "How do I deploy?", created_at: now,
    updated_at: now, awaiting_approvals: 0, turns: 1, tool_calls: 1, usage: { input_tokens: 100, output_tokens: 20 },
    error: null, duration_s: 0.04,
  },
  input: "How do I deploy?", instructions: null, budget: { max_output_tokens: 1024 }, context_policy: {}, tools: ["knowledge_search"],
  memory_access: null, knowledge_access: { collections: ["docs"], labels: [] }, memories: [], passages: [], grounding: null,
  compactions: [], failure: null, cancellation: null, deadline: null,
  answer: "Deploy with make ship [C-0000beef].",
  turns: [
    {
      index: 0,
      model_call: { operation_id: "op", attempts: [{ attempt: 1, outcome: "responded", started_at: now }], stop_reason: "tool_use" },
      tool_calls: [
        { invocation_id: `${id}/turn/0/call/0`, turn: 0, index: 0, tool: "knowledge_search", arguments: { query: "deploy" },
          status: "succeeded", decision: "allow", rule: "knowledge-read", attempts: 1, result: "…", result_truncated: false,
          result_chars: 1 },
      ],
    },
  ],
} as unknown as Detail;

describe("RunDetail", () => {
  it("draws the run as a timeline ending in the answer, with citations marked", async () => {
    stubApi({
      "GET /v1/session": [200, { principal: admin, expires_at: null }],
      [`GET /v1/runs/${id}`]: [200, detail],
      "GET /v1/approvals": [200, []],
    });
    renderAt(<RunDetail />, { route: `/runs/${id}`, path: "/runs/:id" });
    expect(await screen.findByRole("list", { name: "Run timeline" })).toBeInTheDocument();
    expect(screen.getByText("knowledge_search", { selector: ".tool-call .mono" })).toBeInTheDocument();
    expect(screen.getByText("C-0000beef")).toHaveClass("cite");
    expect(screen.getByText(/deterministic rules, not a language model/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Cancel run/ })).not.toBeInTheDocument();
  });

  it("says plainly when a run is not visible", async () => {
    stubApi({
      "GET /v1/session": [200, { principal: admin, expires_at: null }],
      [`GET /v1/runs/${id}`]: [404, { code: "not_found", detail: "no run" }],
    });
    renderAt(<RunDetail />, { route: `/runs/${id}`, path: "/runs/:id" });
    expect(await screen.findByText(/does not exist, or it is not visible to you/)).toBeInTheDocument();
  });
});
