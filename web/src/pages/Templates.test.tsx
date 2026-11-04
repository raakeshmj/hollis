import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Agent, RunDetail as Detail, TemplateDetail as TDetail, TemplateView } from "../api/types";
import { admin, renderAt, stubApi, viewer } from "../test/render";
import { AgentEdit } from "./AgentEdit";
import { Output } from "./RunDetail";
import { TemplateDetail } from "./TemplateDetail";
import { Templates } from "./Templates";

const check = (requirement: string, ok: boolean, hard: boolean) => ({ requirement, ok, hard, detail: `${requirement} detail` });

const reviewer: TemplateView = {
  id: "code-reviewer", version: "1.0.0", name: "Code reviewer", description: "Reviews code read-only.",
  source: "builtin", digest: "sha256:abc", capabilities: ["code-reading", "read-only"], contract: "code-review",
  parameters: [{ name: "focus", description: "What to focus on", default: "correctness", choices: [], pattern: null }],
  examples: ["Review the diff."], ready: true, checks: [check("filesystem", true, true), check("git", false, false)],
  agents: ["reviewer"],
};
const researcher: TemplateView = {
  ...reviewer, id: "researcher", name: "Researcher", contract: "research-brief", capabilities: ["knowledge-research"],
  ready: false, checks: [check("knowledge", false, true)], agents: [],
};
const detail: TDetail = {
  ...reviewer, agents: [], instructions: "You review code.", tools: ["code_search", "code_read"],
  policy: [{ decision: "deny", tools: "*", effects: ["mutating", "destructive"], outside: {}, name: "read-only", reason: "read-only" }],
  budget: null, skills: [], requires: { model: { tool_calling: true }, workspace: { filesystem: true } },
  fits: [
    { deployment: "demo", ok: true, checks: [] },
    { deployment: "tiny", ok: false, checks: [check("tool calling", false, true)] },
  ],
};
const session: Record<string, [number, unknown]> = { "GET /v1/session": [200, { principal: admin, expires_at: null }] };

describe("Templates", () => {
  it("shows each template with what it produces and whether this workspace can run it", async () => {
    stubApi({ ...session, "GET /v1/templates": [200, [reviewer, researcher]] });
    renderAt(<Templates />);
    const gallery = await screen.findByRole("list", { name: "Templates" });
    expect(gallery.children).toHaveLength(2);
    expect(screen.getByRole("link", { name: "Code reviewer" })).toHaveAttribute("href", "/templates/code-reviewer");
    expect(screen.getByText("Ready; no git")).toBeInTheDocument();
    expect(screen.getByText("Needs knowledge")).toBeInTheDocument();
    expect(screen.getByText("research-brief")).toHaveClass("tag");
  });

  it("creates an agent from a template on a model that fits", async () => {
    const calls = stubApi({
      ...session,
      "GET /v1/templates/code-reviewer": [200, detail],
      "GET /v1/contracts": [200, [{ id: "code-review", version: "1.0.0", name: "Code review", description: "Findings on lines read.", rules: ["every finding names a line the run was shown"], tool: "submit_code_review", input_schema: {} }]],
      "GET /v1/collections": [200, []],
      "GET /v1/agents": [200, []],
      "POST /v1/agents/from-template": [201, { name: "code-reviewer" }],
    });
    renderAt(<TemplateDetail />, { route: "/templates/code-reviewer", path: "/templates/:id" });
    expect(await screen.findByText("every finding names a line the run was shown")).toBeInTheDocument();
    const model = screen.getByLabelText("Model");
    expect([...(model as HTMLSelectElement).options].map((o) => o.value)).toEqual(["demo"]);
    fireEvent.change(screen.getByLabelText("focus"), { target: { value: "security" } });
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({
      template: "code-reviewer", name: "code-reviewer", model: "demo", parameters: { focus: "security" }, overrides: {},
    });
  });

  it("does not offer creation to viewers", async () => {
    stubApi({
      "GET /v1/session": [200, { principal: viewer, expires_at: null }],
      "GET /v1/templates/code-reviewer": [200, detail],
      "GET /v1/contracts": [200, []],
    });
    renderAt(<TemplateDetail />, { route: "/templates/code-reviewer", path: "/templates/:id" });
    expect(await screen.findByText("You review code.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create agent" })).not.toBeInTheDocument();
  });
});

describe("Output", () => {
  it("says what was checked when accepted, and lists the problems when rejected", () => {
    const accepted = { contract: "data-analysis", status: "accepted", attempts: 1, data: { answer: "3 rows" }, problems: [] };
    const { unmount } = renderAt(<Output output={accepted as Detail["output"] & object} />);
    expect(screen.getByText(/Accepted: every reference was checked/)).toBeInTheDocument();
    expect(screen.getByText(/"answer": "3 rows"/)).toBeInTheDocument();
    expect(screen.getByText(/still the agent’s claim/)).toBeInTheDocument();
    unmount();
    const rejected = { contract: "code-review", status: "rejected", attempts: 2, data: null, problems: ["a.py:9: never shown"] };
    renderAt(<Output output={rejected as Detail["output"] & object} />);
    expect(screen.getByText("Rejected after 2 attempts")).toBeInTheDocument();
    expect(screen.getByText("a.py:9: never shown")).toBeInTheDocument();
  });
});

describe("AgentEdit with a template-made agent", () => {
  it("keeps the contract, overlay and skills when saved, and offers the upgrade", async () => {
    const agent = {
      name: "reviewer", description: "Mine.", instructions: "Review.", model: "demo", tools: ["code_read"],
      memory: null, knowledge: null, budget: { max_output_tokens: 4096, max_model_turns: 40 }, context: {}, record_episodes: false, delegates: [], delegation: null,
      policy: detail.policy, contract: "code-review", skills: ["ship-it"], created_at: "2026-11-01T10:00:00Z",
      updated_at: "2026-11-01T10:00:00Z", created_by: "admin", version: 2,
      origin: { template: "code-reviewer", version: "1.0.0", digest: "sha256:abc", parameters: { focus: "x" }, overrides: ["description"] },
    } as unknown as Agent;
    const calls = stubApi({
      ...session,
      "GET /v1/agents/reviewer": [200, agent],
      "GET /v1/agents": [200, [agent]],
      "GET /v1/deployments": [200, [{ id: "demo", tasks: ["text_generation"], builtin: true, status: "ready" }]],
      "GET /v1/tools": [200, []],
      "GET /v1/collections": [200, []],
      "GET /v1/contracts": [200, [{ id: "code-review", name: "Code review" }]],
      "GET /v1/skills": [200, { directory: "skills", skills: [{ name: "ship-it", description: "How we ship." }], problems: [] }],
      "GET /v1/runs": [200, { items: [], total: 0 }],
      "GET /v1/agents/reviewer/upgrade": [200, {
        agent: "reviewer", template: "code-reviewer", from_version: "1.0.0", to_version: "1.1.0", from_digest: "sha256:abc",
        to_digest: "sha256:def", up_to_date: false, changes: [{ field: "instructions", before: "a", after: "b" }],
        kept: ["description"], checks: [],
      }],
      "PUT /v1/agents/reviewer": (body) => [200, { ...agent, ...(body as object), version: 3 }],
      "POST /v1/agents/reviewer/upgrade": [200, { ...agent, version: 3 }],
    });
    renderAt(<AgentEdit />, { route: "/agents/reviewer", path: "/agents/:name" });
    expect(await screen.findByText(/is available \(this agent has 1.0.0\)/)).toBeInTheDocument();
    expect(screen.getByText("description", { selector: "dd" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PUT")).toBe(true));
    const saved = calls.find((c) => c.method === "PUT")?.body as Record<string, unknown>;
    expect(saved.contract).toBe("code-review");
    expect(saved.skills).toEqual(["ship-it"]);
    expect(saved.policy).toEqual(detail.policy);
    expect(saved.budget).toEqual({ max_output_tokens: 4096, max_model_turns: 40 });
    fireEvent.click(screen.getByRole("button", { name: "Upgrade to 1.1.0" }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/upgrade") && c.method === "POST")).toBe(true));
    expect(calls.find((c) => c.method === "POST")?.body).toEqual({ digest: "sha256:def", reset: [] });
  });
});
