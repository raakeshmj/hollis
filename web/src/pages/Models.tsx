import { useState } from "react";
import { useDeleteDeployment, useDeployments, useProbe, useSaveDeployment } from "../api/hooks";
import type { DeploymentView, ProbeResult } from "../api/types";
import { Icon } from "../components/Icon";
import { Async, Confirm, Field, FormError, PageHead, Section, StatusDot, errorText, useToast } from "../components/ui";
import { label } from "../lib/format";
import { canOperate, isAdmin, useMe } from "../lib/session";

const PROTOCOLS = [
  ["openai-chat-completions", "OpenAI-compatible chat"],
  ["anthropic-messages", "Anthropic Messages"],
  ["openai-embeddings", "OpenAI-compatible embeddings"],
] as const;

export function Models() {
  const deployments = useDeployments();
  const remove = useDeleteDeployment();
  const probe = useProbe();
  const toast = useToast();
  const me = useMe();
  const admin = isAdmin(me);
  const operator = canOperate(me);
  const [probing, setProbing] = useState<DeploymentView | null>(null);
  const [removing, setRemoving] = useState<DeploymentView | null>(null);
  const [results, setResults] = useState<Record<string, ProbeResult>>({});

  return (
    <>
      <PageHead
        title="Models and deployments"
        description="Agents reference deployments by id. A deployment names a protocol, an endpoint and a model; credentials stay in environment variables and are never stored or shown."
      />
      <Section title="Deployments">
        <Async query={deployments}>
          {(items) => (
            <div className="table-wrap" role="region" aria-label="Deployments" tabIndex={0}>
              <table>
                <thead>
                  <tr>
                    <th scope="col">Deployment</th>
                    <th scope="col">Status</th>
                    <th scope="col">Protocol</th>
                    <th scope="col" className="hide-narrow">
                      Endpoint
                    </th>
                    <th scope="col">Credential</th>
                    <th scope="col">
                      <span className="visually-hidden">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((d) => (
                    <tr key={d.id}>
                      <td>
                        <span className="mono" style={{ fontWeight: 500 }}>
                          {d.id}
                        </span>
                        <div className="small muted">
                          {d.model} · {d.locality} · {d.tasks.map(label).join(", ")}
                        </div>
                        {d.description ? <div className="small muted">{d.description}</div> : null}
                        {results[d.id] ? (
                          <div className="small" style={{ color: results[d.id]?.ok ? "var(--success)" : "var(--danger)" }}>
                            {results[d.id]?.ok ? "Answered" : "Failed"}
                            {results[d.id]?.latency_ms ? ` in ${Math.round(results[d.id]?.latency_ms ?? 0)} ms` : ""}:{" "}
                            {results[d.id]?.detail}
                          </div>
                        ) : null}
                      </td>
                      <td>
                        <StatusDot tone={d.status === "ready" ? "ok" : "warn"}>{label(d.status)}</StatusDot>
                        {d.status_detail ? <div className="small muted">{d.status_detail}</div> : null}
                      </td>
                      <td className="small mono">{d.protocol}</td>
                      <td className="small mono hide-narrow">{d.endpoint ?? <span className="muted">in process</span>}</td>
                      <td className="small">
                        {d.credential ? (
                          <>
                            <span className="mono">${d.credential.env}</span>{" "}
                            <span className={d.credential.present ? "muted" : ""} style={d.credential.present ? undefined : { color: "var(--brass-text)" }}>
                              {d.credential.present ? "set" : "not set"}
                            </span>
                          </>
                        ) : (
                          <span className="muted">none</span>
                        )}
                      </td>
                      <td>
                        <div className="row" style={{ justifyContent: "flex-end" }}>
                          {operator ? (
                            <button type="button" className="btn small" onClick={() => setProbing(d)} disabled={probe.isPending}>
                              Test
                            </button>
                          ) : null}
                          {admin && !d.builtin ? (
                            <button type="button" className="btn small danger" onClick={() => setRemoving(d)}>
                              Remove
                            </button>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Async>
        <p className="small muted" style={{ marginTop: 12 }}>
          Compatibility with a specific server or model is established only by a recorded live run, not by these
          settings.
        </p>
      </Section>
      {admin ? <AddDeployment /> : null}
      <Confirm
        open={probing !== null}
        title={`Test ${probing?.id ?? ""}?`}
        confirmLabel="Send a test request"
        busy={probe.isPending}
        onCancel={() => setProbing(null)}
        onConfirm={() => {
          const target = probing;
          if (!target) return;
          probe.mutate(target.id, {
            onSuccess: (result) => setResults((r) => ({ ...r, [target.id]: result })),
            onError: (error) => toast(errorText(error), "error"),
            onSettled: () => setProbing(null),
          });
        }}
      >
        <p>
          {probing?.builtin
            ? "The demo model answers in process; nothing leaves this machine."
            : "This sends one short request to the deployment’s endpoint. With a paid provider it costs a fraction of a cent."}
        </p>
      </Confirm>
      <Confirm
        open={removing !== null}
        title={`Remove ${removing?.id ?? ""}?`}
        confirmLabel="Remove deployment"
        danger
        busy={remove.isPending}
        onCancel={() => setRemoving(null)}
        onConfirm={() => {
          const target = removing;
          if (!target) return;
          remove.mutate(target.id, {
            onSuccess: () => toast(`Removed ${target.id}.`),
            onError: (error) => toast(errorText(error), "error"),
            onSettled: () => setRemoving(null),
          });
        }}
      >
        <p>It is removed from hollis.toml (a backup is kept). Agents that use it must be changed first.</p>
      </Confirm>
    </>
  );
}

function AddDeployment() {
  const save = useSaveDeployment();
  const toast = useToast();
  const [form, setForm] = useState({
    id: "",
    protocol: "openai-chat-completions",
    provider: "",
    model: "",
    base_url: "http://127.0.0.1:11434/v1",
    locality: "local",
    env: "",
    header: "",
    tools: false,
    window: "",
    dimensions: "",
  });
  const set = (key: keyof typeof form, value: string | boolean) => setForm((f) => ({ ...f, [key]: value }));
  const embeddings = form.protocol === "openai-embeddings";

  const submit = () => {
    const auth = form.env
      ? form.header
        ? { type: "header", header: form.header, env: form.env }
        : { type: "bearer", env: form.env }
      : { type: "none" };
    const capabilities = embeddings
      ? { tasks: ["embedding"], text: null, embedding: { dimensions: Number(form.dimensions) } }
      : { text: { tools: form.tools, ...(form.window ? { context_window_tokens: Number(form.window) } : {}) } };
    save.mutate(
      {
        id: form.id,
        protocol: form.protocol,
        provider: form.provider || (form.protocol === "anthropic-messages" ? "anthropic" : "openai-compatible"),
        model: form.model,
        locality: form.locality,
        endpoint: { base_url: form.base_url, auth },
        capabilities,
      },
      {
        onSuccess: (view) => {
          toast(`Saved ${view.id}: ${label(view.status)}.`);
          setForm((f) => ({ ...f, id: "", model: "" }));
        },
      },
    );
  };

  return (
    <Section title="Add a deployment">
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <div className="form-row">
          <Field label="Id" hint="How agents refer to it, e.g. local-qwen.">
            {(props) => <input {...props} type="text" value={form.id} onChange={(e) => set("id", e.target.value)} required />}
          </Field>
          <Field label="Protocol" hint="OpenAI-compatible covers Ollama, vLLM, llama.cpp, LM Studio and OpenAI.">
            {(props) => (
              <select {...props} value={form.protocol} onChange={(e) => set("protocol", e.target.value)}>
                {PROTOCOLS.map(([value, text]) => (
                  <option key={value} value={value}>
                    {text}
                  </option>
                ))}
              </select>
            )}
          </Field>
        </div>
        <div className="form-row">
          <Field label="Model" hint="The provider’s model name.">
            {(props) => <input {...props} type="text" value={form.model} onChange={(e) => set("model", e.target.value)} required />}
          </Field>
          <Field label="Provider label" hint="Optional: who serves it.">
            {(props) => <input {...props} type="text" value={form.provider} onChange={(e) => set("provider", e.target.value)} />}
          </Field>
        </div>
        <div className="form-row">
          <Field label="Base URL">
            {(props) => (
              <input {...props} type="text" className="code" value={form.base_url} onChange={(e) => set("base_url", e.target.value)} required />
            )}
          </Field>
          <Field label="Locality" hint="Routing policy can allow or block each.">
            {(props) => (
              <select {...props} value={form.locality} onChange={(e) => set("locality", e.target.value)}>
                <option value="local">local (this machine)</option>
                <option value="private">private (your network)</option>
                <option value="external">external (a third party)</option>
              </select>
            )}
          </Field>
        </div>
        <div className="form-row">
          <Field label="API key variable" hint="The environment variable holding the key. Empty: no key.">
            {(props) => (
              <input
                {...props}
                type="text"
                className="code"
                value={form.env}
                onChange={(e) => set("env", e.target.value)}
                pattern="[A-Z_][A-Z0-9_]*"
                placeholder="OPENAI_API_KEY"
              />
            )}
          </Field>
          <Field label="Key header" hint="Optional: send the key in this header instead of Authorization.">
            {(props) => <input {...props} type="text" value={form.header} onChange={(e) => set("header", e.target.value)} placeholder="x-api-key" />}
          </Field>
        </div>
        {embeddings ? (
          <Field label="Dimensions">
            {(props) => <input {...props} type="number" min={1} value={form.dimensions} onChange={(e) => set("dimensions", e.target.value)} required />}
          </Field>
        ) : (
          <div className="form-row">
            <Field label="Context window (tokens)" hint="Optional; enables context budgeting.">
              {(props) => <input {...props} type="number" min={1} value={form.window} onChange={(e) => set("window", e.target.value)} />}
            </Field>
            <label className="check" style={{ alignSelf: "center" }}>
              <input type="checkbox" checked={form.tools} onChange={(e) => set("tools", e.target.checked)} />
              <span>Supports tool calling</span>
            </label>
          </div>
        )}
        <FormError error={save.error} />
        <div className="form-actions">
          <button className="btn primary" type="submit" disabled={save.isPending}>
            <Icon name="plus" size={14} /> Save deployment
          </button>
        </div>
      </form>
    </Section>
  );
}
