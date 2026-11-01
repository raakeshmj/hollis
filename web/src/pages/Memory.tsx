import { useState } from "react";
import { useMemories, useMemory, useMemoryAction, useRemember, useScopes } from "../api/hooks";
import { Icon } from "../components/Icon";
import { Async, Confirm, Empty, ErrorState, Field, FormError, Loading, PageHead, Section, Time, errorText, useToast } from "../components/ui";
import { canOperate, useMe } from "../lib/session";

const KINDS = ["fact", "preference", "procedure", "decision", "observation", "episode"];
const STATUSES = ["active", "superseded", "retracted", "expired"];

export function Memory() {
  const me = useMe();
  const [scope, setScope] = useState("");
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("active");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const scopes = useScopes();
  const memories = useMemories({
    scope: scope || undefined,
    kind: kind || undefined,
    status: status || undefined,
    query: query.trim() || undefined,
  });

  return (
    <>
      <PageHead
        title="Memory"
        description="What agents remember between runs, per scope. Every write goes through the memory policy; you see only scopes you are granted. Forgetting removes a memory from this store, not from run event logs that recalled it."
      />
      <div className="toolbar">
        <label htmlFor="memory-query" className="visually-hidden">
          Search memories
        </label>
        <input id="memory-query" type="search" placeholder="Search" value={query} onChange={(e) => setQuery(e.target.value)} />
        <label className="row small">
          Scope
          <select aria-label="Filter by scope" value={scope} onChange={(e) => setScope(e.target.value)} style={{ width: "auto", minHeight: 28, padding: "2px 8px" }}>
            <option value="">all</option>
            {scopes.data?.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </label>
        <label className="row small">
          Kind
          <select aria-label="Filter by kind" value={kind} onChange={(e) => setKind(e.target.value)} style={{ width: "auto", minHeight: 28, padding: "2px 8px" }}>
            <option value="">all</option>
            {KINDS.map((k) => (
              <option key={k}>{k}</option>
            ))}
          </select>
        </label>
        <label className="row small">
          Status
          <select aria-label="Filter by status" value={status} onChange={(e) => setStatus(e.target.value)} style={{ width: "auto", minHeight: 28, padding: "2px 8px" }}>
            <option value="">all</option>
            {STATUSES.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </label>
      </div>
      <div className="columns">
        <Async
          query={memories}
          empty={(items) =>
            items.length === 0 ? (
              <Empty title="No memories match">Agents with memory access write here as they work; you can also add one below.</Empty>
            ) : null
          }
        >
          {(items) => (
            <div className="table-wrap" role="region" aria-label="Memories" tabIndex={0}>
              <table>
                <thead>
                  <tr>
                    <th scope="col">Memory</th>
                    <th scope="col">Scope</th>
                    <th scope="col" className="hide-narrow">
                      Kind
                    </th>
                    <th scope="col">Recorded</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((m) => (
                    <tr key={m.memory_id} aria-selected={selected === m.memory_id}>
                      <td>
                        <button
                          type="button"
                          className="btn quiet"
                          style={{ padding: 0, height: "auto", textAlign: "left", whiteSpace: "normal", fontWeight: 400, color: "var(--ink)" }}
                          onClick={() => setSelected(m.memory_id)}
                        >
                          {m.content}
                        </button>
                        {m.status !== "active" ? <div className="small muted">{m.status}</div> : null}
                      </td>
                      <td>
                        <span className="tag">{m.scope}</span>
                      </td>
                      <td className="small hide-narrow">{m.kind}</td>
                      <td className="muted nowrap small">
                        <Time value={m.recorded_at} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Async>
        <div>{selected ? <MemoryPanel id={selected} onClose={() => setSelected(null)} /> : <p className="small muted">Select a memory to see where it came from and its history.</p>}</div>
      </div>
      {canOperate(me) ? <Remember scopes={scopes.data ?? []} /> : null}
    </>
  );
}

function MemoryPanel({ id, onClose }: { id: string; onClose: () => void }) {
  const detail = useMemory(id);
  const action = useMemoryAction();
  const toast = useToast();
  const me = useMe();
  const [mode, setMode] = useState<"retract" | "forget" | null>(null);
  const [reason, setReason] = useState("");
  if (detail.isPending) return <Loading rows={5} />;
  if (detail.error) return <ErrorState error={detail.error} />;
  const m = detail.data.memory;
  return (
    <aside className="side-panel" aria-label="Memory detail">
      <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
        <p style={{ fontSize: "var(--text-lg)" }}>{m.content}</p>
        <button type="button" className="btn small quiet" onClick={onClose} aria-label="Close memory">
          <Icon name="close" size={14} />
        </button>
      </div>
      <dl className="facts small">
        <dt>Scope</dt>
        <dd>{m.scope}</dd>
        <dt>Kind</dt>
        <dd>
          {m.kind}
          {m.subject ? ` · ${m.subject}` : ""}
          {m.key ? ` · slot ${m.key}` : ""}
        </dd>
        <dt>Status</dt>
        <dd>
          {m.status}
          {m.status_reason ? ` (${m.status_reason})` : ""}
        </dd>
        <dt>Source</dt>
        <dd>
          {m.source}
          {m.external ? " (external content)" : ""}
        </dd>
        <dt>Certainty</dt>
        <dd>{m.certainty}</dd>
        <dt>Recalled</dt>
        <dd>{m.recall_count} times</dd>
        {m.valid_until ? (
          <>
            <dt>Valid until</dt>
            <dd>
              <Time value={m.valid_until} relative={false} />
            </dd>
          </>
        ) : null}
      </dl>
      {detail.data.history.length ? (
        <div>
          <h3>History</h3>
          <ol className="small" style={{ margin: 0, paddingLeft: 18 }}>
            {detail.data.history.map((h, i) => (
              <li key={i}>
                <Time value={h.at} /> · {h.action} by {h.actor}
                {h.detail ? <span className="muted">: {h.detail}</span> : null}
              </li>
            ))}
          </ol>
        </div>
      ) : null}
      {canOperate(me) && m.status === "active" ? (
        <div className="row">
          <button type="button" className="btn small" onClick={() => setMode("retract")}>
            Retract
          </button>
          <button type="button" className="btn small danger" onClick={() => setMode("forget")}>
            Forget
          </button>
        </div>
      ) : null}
      <Confirm
        open={mode !== null}
        title={mode === "forget" ? "Forget this memory?" : "Retract this memory?"}
        confirmLabel={mode === "forget" ? "Forget" : "Retract"}
        danger={mode === "forget"}
        busy={action.isPending}
        onCancel={() => setMode(null)}
        onConfirm={() =>
          mode &&
          action.mutate(
            { id: m.memory_id, action: mode, reason: reason.trim() || `${mode} in the web app` },
            {
              onSuccess: () => {
                toast(mode === "forget" ? "Forgotten." : "Retracted.");
                if (mode === "forget") onClose();
              },
              onError: (error) => toast(errorText(error), "error"),
              onSettled: () => setMode(null),
            },
          )
        }
      >
        <p>
          {mode === "forget"
            ? "Its content is deleted from the memory store. Run event logs that recalled it still contain it; delete those runs to remove it everywhere."
            : "It stays in the history but is never recalled again."}
        </p>
        <div style={{ marginTop: 12 }}>
          <Field label="Reason (recorded)">
            {(props) => <input {...props} type="text" value={reason} onChange={(e) => setReason(e.target.value)} />}
          </Field>
        </div>
      </Confirm>
    </aside>
  );
}

function Remember({ scopes }: { scopes: string[] }) {
  const remember = useRemember();
  const toast = useToast();
  const [scope, setScope] = useState("");
  const [kind, setKind] = useState("fact");
  const [content, setContent] = useState("");
  return (
    <Section title="Remember something">
      <form
        className="form"
        onSubmit={(event) => {
          event.preventDefault();
          remember.mutate(
            { scope: scope.trim(), kind, content: content.trim() },
            {
              onSuccess: (result) => {
                toast(`Memory ${result.action}${result.reason ? `: ${result.reason}` : ""}.`);
                setContent("");
              },
            },
          );
        }}
      >
        <Field label="Content">
          {(props) => <textarea {...props} rows={3} value={content} onChange={(e) => setContent(e.target.value)} maxLength={4000} required />}
        </Field>
        <div className="form-row">
          <Field label="Scope" hint="kind:id, e.g. team:ops. You can write only scopes you are granted.">
            {(props) => (
              <input {...props} type="text" className="code" list="memory-scopes" value={scope} onChange={(e) => setScope(e.target.value)} required />
            )}
          </Field>
          <Field label="Kind">
            {(props) => (
              <select {...props} value={kind} onChange={(e) => setKind(e.target.value)}>
                {KINDS.filter((k) => k !== "episode").map((k) => (
                  <option key={k}>{k}</option>
                ))}
              </select>
            )}
          </Field>
        </div>
        <datalist id="memory-scopes">
          {scopes.map((s) => (
            <option key={s} value={s} />
          ))}
        </datalist>
        <FormError error={remember.error} />
        <div className="form-actions">
          <button type="submit" className="btn primary" disabled={remember.isPending}>
            Remember
          </button>
        </div>
      </form>
    </Section>
  );
}
