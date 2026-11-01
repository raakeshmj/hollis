import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { ApiError } from "../api/client";
import {
  useAgent,
  useAgents,
  useCollections,
  useDeleteAgent,
  useDeployments,
  useRuns,
  useSaveAgent,
  useTools,
  type AgentDraft,
} from "../api/hooks";
import type { Agent } from "../api/types";
import { Composer, RunTable } from "../components/runs";
import { Async, Confirm, Empty, ErrorState, Field, FormError, Loading, PageHead, Policy, Section, useToast } from "../components/ui";
import { isAdmin, useMe } from "../lib/session";

const BLANK: AgentDraft = {
  name: "",
  description: "",
  instructions: "",
  model: "",
  tools: [],
  memory: null,
  knowledge: null,
  budget: null,
  record_episodes: false,
  delegates: [],
  delegation: null,
};

const LIMITS = [
  { key: "max_depth", label: "Max depth", hint: "Levels of delegation below the root run." },
  { key: "max_children", label: "Delegations per run", hint: "" },
  { key: "max_runs", label: "Runs in total", hint: "In the whole orchestration, root included." },
  { key: "token_budget", label: "Token budget", hint: "For the run and everything it delegates." },
  { key: "wait_s", label: "Wait per delegation (s)", hint: "" },
] as const;

type LimitKey = (typeof LIMITS)[number]["key"];

const list = (value: string) =>
  value
    .split(/[\s,]+/)
    .map((v) => v.trim())
    .filter(Boolean);

function draftOf(agent: Agent): AgentDraft {
  return {
    name: agent.name,
    description: agent.description,
    instructions: agent.instructions,
    model: agent.model,
    tools: [...agent.tools],
    memory: agent.memory ? { read: [...agent.memory.read], write: [...agent.memory.write] } : null,
    knowledge: agent.knowledge
      ? { collections: [...agent.knowledge.collections], labels: [...agent.knowledge.labels] }
      : null,
    budget: agent.budget ? { max_output_tokens: agent.budget.max_output_tokens } : null,
    record_episodes: agent.record_episodes,
    delegates: [...agent.delegates],
    delegation: agent.delegation ? { ...agent.delegation } : null,
  };
}

export function AgentEdit() {
  const { name } = useParams();
  const existing = useAgent(name);
  const me = useMe();
  if (name && existing.isPending) return <Loading rows={6} label="Loading the agent" />;
  if (name && existing.error) {
    if (existing.error instanceof ApiError && existing.error.status === 404)
      return (
        <>
          <PageHead title="Agent not found" crumb={<Link to="/agents">Agents</Link>} />
          <Empty title={`There is no agent named ${name}.`} />
        </>
      );
    return <ErrorState error={existing.error} retry={() => void existing.refetch()} />;
  }
  return <AgentForm key={name ?? "new"} agent={existing.data} readOnly={!isAdmin(me)} />;
}

function AgentForm({ agent, readOnly }: { agent: Agent | undefined; readOnly: boolean }) {
  const navigate = useNavigate();
  const deployments = useDeployments();
  const tools = useTools();
  const collections = useCollections();
  const save = useSaveAgent();
  const remove = useDeleteAgent();
  const toast = useToast();
  const runs = useRuns({ agent: agent?.name, limit: 5 });
  const [draft, setDraft] = useState<AgentDraft>(agent ? draftOf(agent) : BLANK);
  const [memoryRw, setMemoryRw] = useState((agent?.memory?.write ?? []).join(", "));
  const [memoryRo, setMemoryRo] = useState(
    (agent?.memory?.read.filter((s) => !agent.memory?.write.includes(s)) ?? []).join(", "),
  );
  const [labels, setLabels] = useState((agent?.knowledge?.labels ?? []).join(", "));
  const agents = useAgents();
  const [limits, setLimits] = useState<Record<LimitKey, string>>(() => {
    const current = agent?.delegation;
    const value = (key: LimitKey) => {
      const v = current?.[key];
      return v === null || v === undefined ? "" : String(v);
    };
    return {
      max_depth: value("max_depth"),
      max_children: value("max_children"),
      max_runs: value("max_runs"),
      token_budget: value("token_budget"),
      wait_s: value("wait_s"),
    };
  });
  const [confirmDelete, setConfirmDelete] = useState(false);
  const textModels = useMemo(
    () => deployments.data?.filter((d) => d.tasks.includes("text_generation")) ?? [],
    [deployments.data],
  );

  const model = draft.model || textModels[0]?.id || "";
  const set = <K extends keyof AgentDraft>(key: K, value: AgentDraft[K]) => setDraft((d) => ({ ...d, [key]: value }));
  const error = save.error instanceof ApiError ? save.error : null;
  const toolList = tools.data;
  const toolSources = useMemo(() => {
    const groups = new Map<string, NonNullable<typeof toolList>>();
    for (const tool of toolList ?? []) groups.set(tool.source, [...(groups.get(tool.source) ?? []), tool]);
    return [...groups.entries()];
  }, [toolList]);

  const submit = () => {
    const rw = list(memoryRw);
    const ro = list(memoryRo);
    const chosen = draft.knowledge?.collections ?? [];
    const delegation = Object.fromEntries(
      LIMITS.filter(({ key }) => limits[key].trim()).map(({ key }) => [key, Number(limits[key])]),
    );
    const payload: AgentDraft = {
      ...draft,
      model,
      delegation: Object.keys(delegation).length ? delegation : null,
      memory: rw.length || ro.length ? { read: [...new Set([...rw, ...ro])], write: rw } : null,
      knowledge: chosen.length ? { collections: chosen, labels: list(labels) } : null,
    };
    save.mutate(
      { draft: payload, existing: Boolean(agent) },
      {
        onSuccess: (saved) => {
          toast(agent ? `Saved ${saved.name} (version ${saved.version}).` : `Created ${saved.name}.`);
          if (!agent) void navigate(`/agents/${saved.name}`);
        },
      },
    );
  };

  return (
    <>
      <PageHead
        crumb={<Link to="/agents">Agents</Link>}
        title={agent ? agent.name : "New agent"}
        description={
          agent
            ? `Version ${agent.version}, created by ${agent.created_by}.${readOnly ? " Only admins change agents." : ""}`
            : "Define what the agent may use. Every change creates a new version; past runs keep the definition they ran with."
        }
        actions={
          agent && !readOnly ? (
            <button type="button" className="btn danger" onClick={() => setConfirmDelete(true)}>
              Delete
            </button>
          ) : null
        }
      />
      {agent ? (
        <Section title="Run this agent">
          <Composer agent={agent.name} />
        </Section>
      ) : null}
      <Section title={agent ? "Definition" : "Definition"}>
        <form
          className="form"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <fieldset disabled={readOnly} style={{ border: 0, padding: 0, margin: 0, display: "contents" }}>
            <div className="form-row">
              {!agent ? (
                <Field label="Name" hint="Lowercase letters, digits and dashes." error={error?.fieldError("name")}>
                  {(props) => (
                    <input
                      {...props}
                      type="text"
                      value={draft.name}
                      onChange={(event) => set("name", event.target.value)}
                      pattern="[a-z0-9][a-z0-9-]{0,62}"
                      required
                    />
                  )}
                </Field>
              ) : null}
              <Field label="Model" hint={<Link to="/models">Model deployments</Link>} error={error?.fieldError("model")}>
                {(props) => (
                  <select {...props} value={model} onChange={(event) => set("model", event.target.value)} required>
                    {textModels.map((d) => (
                      <option key={d.id} value={d.id}>
                        {d.id}
                        {d.builtin ? " (deterministic rules, not a language model)" : ""}
                        {d.status !== "ready" ? ` (${d.status.replaceAll("_", " ")})` : ""}
                      </option>
                    ))}
                  </select>
                )}
              </Field>
            </div>
            <Field label="Description" error={error?.fieldError("description")}>
              {(props) => (
                <input
                  {...props}
                  type="text"
                  value={draft.description}
                  onChange={(event) => set("description", event.target.value)}
                  maxLength={500}
                />
              )}
            </Field>
            <Field label="Instructions" hint="The system prompt. Retrieved documents and tool output never override it.">
              {(props) => (
                <textarea
                  {...props}
                  rows={6}
                  value={draft.instructions}
                  onChange={(event) => set("instructions", event.target.value)}
                />
              )}
            </Field>
            <div className="field">
              <span>Tools</span>
              <span className="hint">
                The policy decides each call: allowed, held for approval, or denied. Nothing else is offered to the model.
              </span>
              {tools.isPending ? <Loading rows={2} /> : null}
              {toolSources.map(([source, group]) => (
                <fieldset className="choices" key={source}>
                  <legend className="small muted">{source}</legend>
                  {group.map((tool) => (
                    <label className="check small" key={tool.name}>
                      <input
                        type="checkbox"
                        checked={draft.tools.includes(tool.name)}
                        onChange={(event) =>
                          set(
                            "tools",
                            event.target.checked ? [...draft.tools, tool.name] : draft.tools.filter((t) => t !== tool.name),
                          )
                        }
                      />
                      <span>
                        <span className="mono">{tool.name}</span>
                        <br />
                        <Policy decision={tool.policy} />
                      </span>
                    </label>
                  ))}
                </fieldset>
              ))}
            </div>
            <div className="form-row">
              <Field label="Memory it reads and writes" hint="Scopes such as team:docs, separated by commas.">
                {(props) => (
                  <input {...props} type="text" className="code" value={memoryRw} onChange={(e) => setMemoryRw(e.target.value)} />
                )}
              </Field>
              <Field label="Memory it only reads">
                {(props) => (
                  <input {...props} type="text" className="code" value={memoryRo} onChange={(e) => setMemoryRo(e.target.value)} />
                )}
              </Field>
            </div>
            <fieldset className="choices">
              <legend>Knowledge collections</legend>
              {collections.data?.length ? (
                collections.data.map((c) => (
                  <label className="check small" key={c.name}>
                    <input
                      type="checkbox"
                      checked={draft.knowledge?.collections.includes(c.name) ?? false}
                      onChange={(event) => {
                        const current = draft.knowledge?.collections ?? [];
                        const next = event.target.checked ? [...current, c.name] : current.filter((n) => n !== c.name);
                        set("knowledge", { collections: next, labels: draft.knowledge?.labels ?? [] });
                      }}
                    />
                    <span>
                      {c.name} <span className="muted">({c.sources} documents)</span>
                    </span>
                  </label>
                ))
              ) : (
                <span className="small muted">No collections yet.</span>
              )}
            </fieldset>
            <div className="form-row">
              <Field label="Access labels" hint="Documents with labels are visible only with a matching label.">
                {(props) => (
                  <input {...props} type="text" className="code" value={labels} onChange={(e) => setLabels(e.target.value)} />
                )}
              </Field>
              <Field label="Output tokens per model call" hint="Empty: the workspace default.">
                {(props) => (
                  <input
                    {...props}
                    type="number"
                    min={1}
                    value={draft.budget?.max_output_tokens ?? ""}
                    onChange={(event) =>
                      set("budget", event.target.value ? { max_output_tokens: Number(event.target.value) } : null)
                    }
                  />
                )}
              </Field>
            </div>
            <fieldset className="choices">
              <legend>Delegates</legend>
              <span className="hint small muted">
                Agents it may hand bounded tasks to, one tool each. Their runs get their own configuration, narrowed to
                what this run may reach; delegations that can change things need approval under the default policy.
              </span>
              {agents.data?.filter((a) => a.name !== agent?.name).length ? (
                agents.data
                  .filter((a) => a.name !== agent?.name)
                  .map((a) => (
                    <label className="check small" key={a.name}>
                      <input
                        type="checkbox"
                        checked={draft.delegates.includes(a.name)}
                        onChange={(event) =>
                          set(
                            "delegates",
                            event.target.checked
                              ? [...draft.delegates, a.name]
                              : draft.delegates.filter((d) => d !== a.name),
                          )
                        }
                      />
                      <span>
                        {a.name}
                        {a.description ? <span className="muted"> — {a.description}</span> : null}
                      </span>
                    </label>
                  ))
              ) : (
                <span className="small muted">No other agents yet.</span>
              )}
            </fieldset>
            {draft.delegates.length || draft.delegation ? (
              <div className="form-row">
                {LIMITS.map(({ key, label: text, hint }) => (
                  <Field key={key} label={text} hint={hint || "Empty: the workspace limit."}>
                    {(props) => (
                      <input
                        {...props}
                        type="number"
                        min={key === "max_depth" || key === "max_children" ? 0 : 1}
                        value={limits[key]}
                        onChange={(event) => setLimits((current) => ({ ...current, [key]: event.target.value }))}
                      />
                    )}
                  </Field>
                ))}
              </div>
            ) : null}
            <label className="check">
              <input
                type="checkbox"
                checked={draft.record_episodes}
                onChange={(event) => set("record_episodes", event.target.checked)}
              />
              <span>
                Record an episode after each run
                <br />
                <span className="small muted">
                  A short memory of what the run did, written to the first scope it may write.
                </span>
              </span>
            </label>
          </fieldset>
          <FormError error={save.error} />
          {!readOnly ? (
            <div className="form-actions">
              <button className="btn primary" type="submit" disabled={save.isPending}>
                {agent ? "Save changes" : "Create agent"}
              </button>
              <Link className="btn" to="/agents">
                Cancel
              </Link>
            </div>
          ) : null}
        </form>
      </Section>
      {agent ? (
        <Section title="Recent runs">
          <Async
            query={runs}
            empty={(page) => (page.items.length === 0 ? <p className="muted">This agent has not run yet.</p> : null)}
          >
            {(page) => <RunTable runs={page.items} showAgent={false} />}
          </Async>
        </Section>
      ) : null}
      {agent ? (
        <Confirm
          open={confirmDelete}
          title={`Delete ${agent.name}?`}
          confirmLabel="Delete agent"
          danger
          busy={remove.isPending}
          onCancel={() => setConfirmDelete(false)}
          onConfirm={() =>
            remove.mutate(agent.name, {
              onSuccess: () => {
                toast(`Deleted ${agent.name}.`);
                void navigate("/agents");
              },
              onSettled: () => setConfirmDelete(false),
            })
          }
        >
          <p>Its past runs stay, with the definition they ran with.</p>
        </Confirm>
      ) : null}
    </>
  );
}
