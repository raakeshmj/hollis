import { useState } from "react";
import {
  useAgents,
  useAudit,
  useCreatePrincipal,
  useCreateToken,
  useHealth,
  usePrincipals,
  useRevokeToken,
  useSettings,
  useTokens,
  useUpdateConfig,
} from "../api/hooks";
import type { CreatedToken, TokenView } from "../api/types";
import {
  Async,
  Confirm,
  CopyButton,
  Field,
  FormError,
  PageHead,
  Section,
  Tags,
  Time,
  errorText,
  useToast,
} from "../components/ui";
import { bytes } from "../lib/format";
import { isAdmin, useMe } from "../lib/session";

export function Settings() {
  const me = useMe();
  const admin = isAdmin(me);
  return (
    <>
      <PageHead title="Settings" description="This workspace, your identity and access, and who can sign in." />
      <Workspace />
      <You />
      <Tokens />
      {admin ? <Configuration /> : null}
      {admin ? <Principals /> : null}
      {admin ? <Audit /> : null}
    </>
  );
}

function Workspace() {
  const health = useHealth();
  const settings = useSettings();
  return (
    <Section title="Workspace">
      <Async query={health}>
        {(h) => (
          <dl className="facts">
            <dt>Name</dt>
            <dd>{h.workspace}</dd>
            <dt>Directory</dt>
            <dd className="mono small">{h.workspace_path}</dd>
            <dt>Configuration</dt>
            <dd className="mono small">{settings.data?.config_path}</dd>
            <dt>Version</dt>
            <dd>
              Hollis {h.version} · API v{h.api_version}
            </dd>
            <dt>Environment</dt>
            <dd>{h.environment}</dd>
            <dt>Storage</dt>
            <dd className="small">
              {h.storage.map((s) => `${s.name} ${bytes(s.bytes)}`).join(" · ")}
            </dd>
          </dl>
        )}
      </Async>
    </Section>
  );
}

function You() {
  const me = useMe();
  if (!me) return null;
  const admin = me.role === "admin";
  return (
    <Section title="You">
      <dl className="facts">
        <dt>Principal</dt>
        <dd>{me.name}</dd>
        <dt>Role</dt>
        <dd>
          {me.role}
          {me.role === "viewer" ? " (reads only)" : me.role === "operator" ? " (runs agents, manages own runs)" : " (everything)"}
        </dd>
        <dt>Approves tool calls</dt>
        <dd>{admin || me.can_approve ? "yes, in runs you can see" : "no"}</dd>
        <dt>Memory</dt>
        <dd>{admin ? "all scopes" : <Tags values={me.memory?.read ?? []} />}</dd>
        <dt>Knowledge</dt>
        <dd>{admin ? "all collections" : <Tags values={me.knowledge?.collections ?? []} />}</dd>
      </dl>
    </Section>
  );
}

function Tokens() {
  const me = useMe();
  const admin = isAdmin(me);
  const tokens = useTokens();
  const principals = usePrincipals(admin);
  const create = useCreateToken();
  const revoke = useRevokeToken();
  const toast = useToast();
  const [labelText, setLabelText] = useState("");
  const [days, setDays] = useState("");
  const [owner, setOwner] = useState("");
  const [created, setCreated] = useState<CreatedToken | null>(null);
  const [revoking, setRevoking] = useState<TokenView | null>(null);

  return (
    <Section title="API tokens" aside={<p>For the SDK, the CLI (HOLLIS_TOKEN) and scripts.</p>}>
      {created ? (
        <div className="notice" role="status" style={{ marginBottom: 16 }}>
          <div style={{ flex: 1 }}>
            <p>
              <strong>Copy this token now.</strong> It is stored only as a hash and cannot be shown again.
            </p>
            <div className="secret" style={{ marginTop: 8 }}>
              <code>{created.token}</code>
              <CopyButton value={created.token} />
            </div>
            <p className="small muted" style={{ marginTop: 8 }}>
              <button type="button" className="btn small quiet" onClick={() => setCreated(null)}>
                I have stored it
              </button>
            </p>
          </div>
        </div>
      ) : null}
      <Async query={tokens} empty={(items) => (items.length === 0 ? <p className="muted">No API tokens.</p> : null)}>
        {(items) => (
          <div className="table-wrap" role="region" aria-label="API tokens" tabIndex={0}>
            <table>
              <thead>
                <tr>
                  <th scope="col">Token</th>
                  <th scope="col">Principal</th>
                  <th scope="col">Label</th>
                  <th scope="col">Last used</th>
                  <th scope="col">Expires</th>
                  <th scope="col">
                    <span className="visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((t) => (
                  <tr key={t.token_id}>
                    <td className="mono small">{t.token_id}</td>
                    <td>{t.principal}</td>
                    <td>{t.label || <span className="muted">-</span>}</td>
                    <td className="muted small">{t.last_used_at ? <Time value={t.last_used_at} /> : "never"}</td>
                    <td className="muted small">{t.expires_at ? <Time value={t.expires_at} relative={false} /> : "never"}</td>
                    <td>
                      <button type="button" className="btn small danger" onClick={() => setRevoking(t)}>
                        Revoke
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Async>
      <form
        className="form"
        style={{ marginTop: 16 }}
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate(
            { principal: owner || me?.name || "", label: labelText.trim(), expires_in_days: days ? Number(days) : null },
            {
              onSuccess: (token) => {
                setCreated(token);
                setLabelText("");
                setDays("");
              },
            },
          );
        }}
      >
        <div className="form-row">
          <Field label="Label" hint="What it is for, e.g. ci or laptop.">
            {(props) => <input {...props} type="text" value={labelText} onChange={(e) => setLabelText(e.target.value)} maxLength={80} />}
          </Field>
          <Field label="Expires after (days)" hint="Empty: never.">
            {(props) => <input {...props} type="number" min={1} max={3650} value={days} onChange={(e) => setDays(e.target.value)} />}
          </Field>
          {admin ? (
            <Field label="For">
              {(props) => (
                <select {...props} value={owner || me?.name} onChange={(e) => setOwner(e.target.value)}>
                  {principals.data?.map((p) => (
                    <option key={p.name}>{p.name}</option>
                  ))}
                </select>
              )}
            </Field>
          ) : null}
        </div>
        <FormError error={create.error} />
        <div className="form-actions">
          <button type="submit" className="btn primary" disabled={create.isPending}>
            Create token
          </button>
        </div>
      </form>
      <Confirm
        open={revoking !== null}
        title={`Revoke ${revoking?.token_id ?? ""}?`}
        confirmLabel="Revoke"
        danger
        busy={revoke.isPending}
        onCancel={() => setRevoking(null)}
        onConfirm={() => {
          const target = revoking;
          if (!target) return;
          revoke.mutate(target.token_id, {
            onSuccess: () => toast(`Revoked ${target.token_id}.`),
            onError: (error) => toast(errorText(error), "error"),
            onSettled: () => setRevoking(null),
          });
        }}
      >
        <p>Anything using it stops working immediately.</p>
      </Confirm>
    </Section>
  );
}

function Configuration() {
  const settings = useSettings();
  const agents = useAgents();
  const update = useUpdateConfig();
  const toast = useToast();
  const config = settings.data?.config as
    | {
        workspace: { name: string };
        server: { run_visibility: string };
        runtime: { environment: string; max_output_tokens: number; default_agent: string | null };
        demo: boolean;
      }
    | undefined;
  const [form, setForm] = useState<{
    name: string;
    visibility: string;
    environment: string;
    tokens: string;
    agent: string;
    demo: boolean;
  } | null>(null);
  if (!config) return null;
  const value = form ?? {
    name: config.workspace.name,
    visibility: config.server.run_visibility,
    environment: config.runtime.environment,
    tokens: String(config.runtime.max_output_tokens),
    agent: config.runtime.default_agent ?? "",
    demo: config.demo,
  };
  const set = (key: keyof typeof value, v: string | boolean) => setForm({ ...value, [key]: v });

  return (
    <Section title="Configuration" aside={<p>Saved to hollis.toml (the previous file is kept as hollis.toml.bak).</p>}>
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          update.mutate(
            {
              workspace: { name: value.name },
              server: { run_visibility: value.visibility },
              runtime: {
                environment: value.environment,
                max_output_tokens: Number(value.tokens),
                default_agent: value.agent || null,
              },
              demo: value.demo,
            },
            {
              onSuccess: () => {
                setForm(null);
                toast("Configuration saved.");
              },
            },
          );
        }}
      >
        <div className="form-row">
          <Field label="Workspace name">
            {(props) => <input {...props} type="text" value={value.name} onChange={(e) => set("name", e.target.value)} required />}
          </Field>
          <Field label="Environment label" hint="Policy rules can match it, e.g. production.">
            {(props) => <input {...props} type="text" value={value.environment} onChange={(e) => set("environment", e.target.value)} required />}
          </Field>
        </div>
        <div className="form-row">
          <Field label="Who can see a run" hint="Admins always can.">
            {(props) => (
              <select {...props} value={value.visibility} onChange={(e) => set("visibility", e.target.value)}>
                <option value="owner">its creator</option>
                <option value="workspace">everyone in the workspace</option>
              </select>
            )}
          </Field>
          <Field label="Default agent" hint="Used when a run names no agent.">
            {(props) => (
              <select {...props} value={value.agent} onChange={(e) => set("agent", e.target.value)}>
                <option value="">none</option>
                {agents.data?.map((a) => (
                  <option key={a.name}>{a.name}</option>
                ))}
              </select>
            )}
          </Field>
          <Field label="Default output tokens per call" hint="For agents without their own budget.">
            {(props) => (
              <input {...props} type="number" min={1} value={value.tokens} onChange={(e) => set("tokens", e.target.value)} required />
            )}
          </Field>
        </div>
        <label className="check">
          <input type="checkbox" checked={value.demo} onChange={(e) => set("demo", e.target.checked)} />
          <span>
            Offer the built-in demo model
            <br />
            <span className="small muted">Deterministic rules, not a language model; agents using it fail if it is off.</span>
          </span>
        </label>
        <FormError error={update.error} />
        <div className="form-actions">
          <button type="submit" className="btn primary" disabled={update.isPending || form === null}>
            Save configuration
          </button>
          {form ? (
            <button type="button" className="btn" onClick={() => setForm(null)}>
              Discard changes
            </button>
          ) : null}
        </div>
      </form>
      <details style={{ marginTop: 20 }}>
        <summary>Effective configuration</summary>
        <pre className="code-block">{JSON.stringify(settings.data?.config, null, 2)}</pre>
      </details>
    </Section>
  );
}

function Principals() {
  const principals = usePrincipals(true);
  const create = useCreatePrincipal();
  const toast = useToast();
  const [form, setForm] = useState({ name: "", role: "operator", approve: false, memory: "", collections: "", labels: "" });
  const set = (key: keyof typeof form, v: string | boolean) => setForm((f) => ({ ...f, [key]: v }));
  const words = (v: string) => v.split(/[\s,]+/).filter(Boolean);
  return (
    <Section title="People and services" aside={<p>Principals sign in with tokens you create for them.</p>}>
      <Async query={principals}>
        {(items) => (
          <div className="table-wrap" role="region" aria-label="Principals" tabIndex={0}>
            <table>
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Role</th>
                  <th scope="col">Approves</th>
                  <th scope="col">Memory</th>
                  <th scope="col">Knowledge</th>
                </tr>
              </thead>
              <tbody>
                {items.map((p) => (
                  <tr key={p.name}>
                    <td>{p.name}</td>
                    <td>{p.role}</td>
                    <td>{p.role === "admin" || p.can_approve ? "yes" : "no"}</td>
                    <td>{p.role === "admin" ? "all" : <Tags values={p.memory?.read ?? []} />}</td>
                    <td>{p.role === "admin" ? "all" : <Tags values={p.knowledge?.collections ?? []} />}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Async>
      <form
        className="form"
        style={{ marginTop: 16 }}
        onSubmit={(event) => {
          event.preventDefault();
          const memory = words(form.memory);
          const collections = words(form.collections);
          create.mutate(
            {
              name: form.name.trim(),
              role: form.role,
              can_approve: form.approve,
              memory: memory.length ? { read: memory, write: memory } : null,
              knowledge: collections.length ? { collections, labels: words(form.labels) } : null,
            },
            {
              onSuccess: (p) => {
                toast(`Created ${p.name}. Create a token for them under API tokens.`);
                setForm({ name: "", role: "operator", approve: false, memory: "", collections: "", labels: "" });
              },
            },
          );
        }}
      >
        <div className="form-row">
          <Field label="Name">
            {(props) => <input {...props} type="text" value={form.name} onChange={(e) => set("name", e.target.value)} pattern="[a-z0-9][a-z0-9-]{0,62}" required />}
          </Field>
          <Field label="Role">
            {(props) => (
              <select {...props} value={form.role} onChange={(e) => set("role", e.target.value)}>
                <option value="viewer">viewer: reads</option>
                <option value="operator">operator: runs agents</option>
                <option value="admin">admin: everything</option>
              </select>
            )}
          </Field>
        </div>
        <div className="form-row">
          <Field label="Memory scopes" hint="Read and write, e.g. team:ops.">
            {(props) => <input {...props} type="text" className="code" value={form.memory} onChange={(e) => set("memory", e.target.value)} />}
          </Field>
          <Field label="Knowledge collections">
            {(props) => <input {...props} type="text" className="code" value={form.collections} onChange={(e) => set("collections", e.target.value)} />}
          </Field>
          <Field label="Knowledge labels">
            {(props) => <input {...props} type="text" className="code" value={form.labels} onChange={(e) => set("labels", e.target.value)} />}
          </Field>
        </div>
        <label className="check">
          <input type="checkbox" checked={form.approve} onChange={(e) => set("approve", e.target.checked)} />
          <span>May approve tool calls in runs they can see</span>
        </label>
        <FormError error={create.error} />
        <div className="form-actions">
          <button type="submit" className="btn primary" disabled={create.isPending}>
            Add principal
          </button>
        </div>
      </form>
    </Section>
  );
}

function Audit() {
  const audit = useAudit(true);
  return (
    <Section title="Audit log" aside={<p>Append-only: who did what, and when.</p>}>
      <Async query={audit} empty={(items) => (items.length === 0 ? <p className="muted">Nothing recorded yet.</p> : null)}>
        {(items) => (
          <div className="table-wrap" role="region" aria-label="Audit log" tabIndex={0}>
            <table>
              <thead>
                <tr>
                  <th scope="col">When</th>
                  <th scope="col">Who</th>
                  <th scope="col">Action</th>
                  <th scope="col">Target</th>
                  <th scope="col">Detail</th>
                </tr>
              </thead>
              <tbody>
                {items.slice(0, 100).map((e, i) => (
                  <tr key={i}>
                    <td className="muted nowrap small">
                      <Time value={e.at} />
                    </td>
                    <td>{e.principal}</td>
                    <td className="mono small">{e.action}</td>
                    <td className="mono small clip" title={e.target}>
                      {e.target}
                    </td>
                    <td className="small">{e.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Async>
    </Section>
  );
}
