import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { ApiError } from "../api/client";
import {
  useAgent,
  useAgents,
  useCollections,
  useContracts,
  useDeleteAgent,
  useDeployments,
  useExportAgent,
  useRuns,
  useSaveAgent,
  useSkills,
  useTools,
  useUpgradeAgent,
  useUpgradePreview,
  type AgentDraft,
} from "../api/hooks";
import type { Agent, AgentOrigin } from "../api/types";
import { Rules } from "./TemplateDetail";
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
  policy: [],
  contract: null,
  skills: [],
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
    budget: agent.budget ? { ...agent.budget } : null,
    record_episodes: agent.record_episodes,
    delegates: [...agent.delegates],
    delegation: agent.delegation ? { ...agent.delegation } : null,
    policy: [...(agent.policy ?? [])],
    contract: agent.contract ?? null,
    skills: [...(agent.skills ?? [])],
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
  const contracts = useContracts();
  const skills = useSkills();
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
      {agent?.origin ? <Origin agent={agent} origin={agent.origin} readOnly={readOnly} /> : null}
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
                      set(
                        "budget",
                        event.target.value
                          ? { ...(draft.budget ?? {}), max_output_tokens: Number(event.target.value) }
                          : null,
                      )
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
            <div className="form-row">
              <Field
                label="Output contract"
                hint="Runs submit a structured result that Hollis checks against the run’s own log."
              >
                {(props) => (
                  <select
                    {...props}
                    value={draft.contract ?? ""}
                    onChange={(event) => set("contract", event.target.value || null)}
                  >
                    <option value="">None (free-text answer)</option>
                    {contracts.data?.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name} ({c.id})
                      </option>
                    ))}
                  </select>
                )}
              </Field>
            </div>
            <fieldset className="choices">
              <legend>Skills it is offered</legend>
              <span className="hint small muted">
                Agent Skills in this workspace, offered by name and read on demand. Nothing in a skill is executed or
                grants permissions.
              </span>
              {skills.data?.skills.length ? (
                skills.data.skills.map((skill) => (
                  <label className="check small" key={skill.name}>
                    <input
                      type="checkbox"
                      checked={draft.skills.includes(skill.name)}
                      onChange={(event) =>
                        set(
                          "skills",
                          event.target.checked
                            ? [...draft.skills, skill.name]
                            : draft.skills.filter((s) => s !== skill.name),
                        )
                      }
                    />
                    <span>
                      {skill.name}
                      <span className="muted"> — {skill.description}</span>
                    </span>
                  </label>
                ))
              ) : (
                <span className="small muted">No skills in this workspace.</span>
              )}
            </fieldset>
            <div className="field">
              <span>Policy overlay</span>
              <span className="hint">
                Rules that make the workspace policy stricter for this agent’s runs and every run it delegates. They can
                never allow more.
              </span>
              {draft.policy.length ? (
                <>
                  <Rules rules={draft.policy} />
                  {!readOnly ? (
                    <div className="row">
                      {draft.policy.map((rule, index) => (
                        <button
                          key={`${rule.name}-${index}`}
                          type="button"
                          className="btn small"
                          onClick={() => set("policy", draft.policy.filter((_, i) => i !== index))}
                        >
                          Remove {rule.name || `rule ${index + 1}`}
                        </button>
                      ))}
                    </div>
                  ) : null}
                </>
              ) : (
                <span className="small muted">None: the workspace policy alone decides.</span>
              )}
            </div>
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

const FIELD_LABELS: Record<string, string> = {
  description: "description",
  instructions: "instructions",
  tools: "tools",
  contract: "output contract",
  policy: "policy overlay",
  skills: "skills",
  budget: "budget",
};

/** Where a template-made agent came from: customizations, upgrades, export. */
function Origin({ agent, origin, readOnly }: { agent: Agent; origin: AgentOrigin; readOnly: boolean }) {
  const preview = useUpgradePreview(agent.name, !readOnly);
  const upgrade = useUpgradeAgent();
  const exporter = useExportAgent();
  const toast = useToast();
  const p = preview.data;
  return (
    <Section
      title="Template"
      aside={
        !readOnly ? (
          <button
            type="button"
            className="btn small"
            disabled={exporter.isPending}
            onClick={() =>
              exporter.mutate(
                { name: agent.name },
                {
                  onSuccess: (file) => {
                    const url = URL.createObjectURL(new Blob([file.content], { type: "application/toml" }));
                    const link = document.createElement("a");
                    link.href = url;
                    link.download = file.filename;
                    link.click();
                    URL.revokeObjectURL(url);
                    toast(`Exported ${file.filename}.`);
                  },
                },
              )
            }
          >
            Export as template
          </button>
        ) : null
      }
    >
      <dl className="facts small">
        <dt>Made from</dt>
        <dd>
          <Link to={`/templates/${origin.template}`}>{origin.template}</Link> {origin.version}{" "}
          <span className="mono muted">{origin.digest}</span>
        </dd>
        {Object.keys(origin.parameters ?? {}).length ? (
          <>
            <dt>Parameters</dt>
            <dd className="mono">
              {Object.entries(origin.parameters ?? {})
                .map(([k, v]) => `${k}=${v || "\"\""}`)
                .join(", ")}
            </dd>
          </>
        ) : null}
        <dt>Customized</dt>
        <dd>
          {origin.overrides?.length
            ? origin.overrides.map((f) => FIELD_LABELS[f] ?? f).join(", ")
            : "nothing: it matches its template"}
        </dd>
      </dl>
      {p && !p.up_to_date ? (
        <div className="notice" style={{ marginTop: 16 }}>
          <div>
            <p>
              <strong>
                {p.template} {p.to_version}
              </strong>{" "}
              is available (this agent has {p.from_version}). Upgrading changes{" "}
              {p.changes.length ? p.changes.map((c) => FIELD_LABELS[c.field] ?? c.field).join(", ") : "nothing visible"}
              {p.kept.length ? `, and keeps your ${p.kept.map((f) => FIELD_LABELS[f] ?? f).join(", ")}` : ""}.
            </p>
            {p.checks.filter((c) => c.hard && !c.ok).map((c) => (
              <p className="small" key={c.requirement}>
                Blocked: {c.requirement}: {c.detail}
              </p>
            ))}
            <button
              type="button"
              className="btn primary small"
              disabled={upgrade.isPending}
              onClick={() =>
                upgrade.mutate(
                  { name: agent.name, digest: p.to_digest, reset: [] },
                  { onSuccess: (saved) => toast(`Upgraded ${saved.name} to ${p.to_version}.`) },
                )
              }
            >
              Upgrade to {p.to_version}
            </button>
            <FormError error={upgrade.error} />
          </div>
        </div>
      ) : p ? (
        <p className="small muted" style={{ marginTop: 12 }}>
          Up to date with its template.
        </p>
      ) : null}
    </Section>
  );
}
