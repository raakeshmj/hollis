import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Approval } from "../api/types";
import { admin, renderAt, stubApi, viewer } from "../test/render";
import { ApprovalItem } from "./runs";

const approval: Approval = {
  run_id: "11111111-2222-4333-8444-555555555555",
  agent: "helper",
  invocation_id: "11111111-2222-4333-8444-555555555555/turn/0/call/0",
  tool: "fs_write_file",
  effect: "mutating",
  trust: "trusted",
  idempotent: false,
  arguments: { path: "notes.txt", content: "hello" },
  reason: "trusted mutating tool requires approval",
  binding: "sha256:abc",
  expires_at: "2099-01-01T00:00:00Z",
  requested_at: null,
  run_input: "write notes.txt: hello",
  root_run_id: null,
  depth: 0,
  lineage: ["helper"],
};

const summary = { run_id: approval.run_id, phase: "running" };

describe("ApprovalItem", () => {
  it("records the decision for exactly this invocation, with the reason", async () => {
    const calls = stubApi({
      "GET /v1/session": [200, { principal: admin, expires_at: null }],
      [`POST /v1/runs/${approval.run_id}/approvals/${approval.run_id}/turn/0/call/0`]: [200, summary],
    });
    renderAt(<ApprovalItem approval={approval} />);
    await userEvent.type(await screen.findByPlaceholderText(/Reason/), "looks right");
    await userEvent.click(screen.getByRole("button", { name: /Approve/ }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const decision = calls.find((c) => c.method === "POST");
    expect(decision?.url).toBe(`/v1/runs/${approval.run_id}/approvals/${approval.run_id}/turn/0/call/0`);
    expect(decision?.body).toEqual({ approved: true, reason: "looks right" });
  });

  it("offers no decision to someone without the approve permission", async () => {
    stubApi({ "GET /v1/session": [200, { principal: viewer, expires_at: null }] });
    renderAt(<ApprovalItem approval={approval} />);
    expect(await screen.findByText(/cannot approve tool calls/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve/ })).not.toBeInTheDocument();
    expect(screen.getByText("notes.txt")).toBeInTheDocument();
  });
});
