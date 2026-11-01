import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { useAgents, useDecide, useDeployments, useStartRun } from "../api/hooks";
import type { Approval, RunSummary } from "../api/types";
import { callLabel, count, duration, label, runner, shortId } from "../lib/format";
import { canApprove, canOperate, useMe } from "../lib/session";
import { Icon } from "./Icon";
import { LineagePath } from "./orchestration";
import { Arguments, FormError, Phase, Time, errorText, useToast } from "./ui";

/** Start a run: the task, the agent, and (optionally) a session for shared memory. */
export function Composer({ agent: preset }: { agent?: string }) {
  const me = useMe();
  const agents = useAgents();
  const start = useStartRun();
  const navigate = useNavigate();
  const [input, setInput] = useState("");
  const [agent, setAgent] = useState(preset ?? "");
  const [session, setSession] = useState("");
  const names = agents.data?.map((a) => a.name) ?? [];
  const chosen = agent || names[0] || "";

  if (!canOperate(me)) return null;
  if (agents.data && names.length === 0) return null;

  const submit = () => {
    if (!input.trim() || !chosen) return;
    start.mutate(
      { input: input.trim(), agent: chosen, session: session.trim() || undefined },
      { onSuccess: (run) => void navigate(`/runs/${run.run_id}`) },
    );
  };

  return (
    <form
      className="stack"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <div className="composer">
        <label htmlFor="composer-input" className="visually-hidden">
          Task for the agent
        </label>
        <textarea
          id="composer-input"
          value={input}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              submit();
            }
          }}
          placeholder="What should the agent do?"
        />
        <div className="controls">
          {preset ? (
            <span>
              Agent <strong>{preset}</strong>
            </span>
          ) : (
            <label className="row">
              Agent
              <select value={chosen} onChange={(event) => setAgent(event.target.value)}>
                {names.map((name) => (
                  <option key={name}>{name}</option>
                ))}
              </select>
            </label>
          )}
          <label className="row">
            Session
            <input
              type="text"
              value={session}
              onChange={(event) => setSession(event.target.value)}
              placeholder="optional"
              pattern="[A-Za-z0-9._-]{1,64}"
              title="Letters, digits, dot, dash and underscore"
              style={{ width: 120, minHeight: 28, padding: "2px 8px" }}
            />
          </label>
        </div>
        <div className="row" style={{ justifyContent: "flex-end" }}>
          <span className="muted small hide-narrow">⌘↵</span>
          <button className="btn primary" type="submit" disabled={start.isPending || !input.trim() || !chosen}>
            <Icon name="play" size={14} /> Start run
          </button>
        </div>
      </div>
      <FormError error={start.error} />
    </form>
  );
}

export function RunTable({ runs, showAgent = true }: { runs: RunSummary[]; showAgent?: boolean }) {
  return (
    <div className="table-wrap" role="region" aria-label="Runs" tabIndex={0}>
      <table>
        <thead>
          <tr>
            <th scope="col">Run</th>
            <th scope="col">Phase</th>
            {showAgent ? <th scope="col">Agent</th> : null}
            <th scope="col">Task</th>
            <th scope="col" className="num hide-narrow">
              Tools
            </th>
            <th scope="col" className="hide-narrow">
              Duration
            </th>
            <th scope="col">Updated</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => (
            <tr key={run.run_id}>
              <td>
                <Link className="row-link mono" to={`/runs/${run.run_id}`}>
                  {shortId(run.run_id)}
                </Link>
              </td>
              <td>
                <Phase phase={run.phase} />
              </td>
              {showAgent ? (
                <td>
                  {runner(run) ?? <span className="muted">ad hoc</span>}
                  {run.children ? <span className="muted small"> +{run.children} delegated</span> : null}
                </td>
              ) : null}
              <td className="clip" title={run.objective ?? run.input_preview}>
                {run.depth ? <span className="muted small">↳ </span> : null}
                {run.objective ?? run.input_preview}
              </td>
              <td className="num hide-narrow">{run.tool_calls}</td>
              <td className="hide-narrow muted">{duration(run.duration_s)}</td>
              <td className="nowrap muted">
                <Time value={run.updated_at} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A pending tool call: what it would do, and the decision, made as the signed-in principal. */
export function ApprovalItem({ approval, showRun = true }: { approval: Approval; showRun?: boolean }) {
  const me = useMe();
  const decide = useDecide();
  const toast = useToast();
  const [reason, setReason] = useState("");
  const allowed = canApprove(me);
  const act = (approved: boolean) =>
    decide.mutate(
      { approval, approved, reason: reason.trim() || undefined },
      {
        onSuccess: () => toast(`${approval.tool} ${approved ? "approved" : "denied"}.`),
        onError: (error) => toast(errorText(error), "error"),
      },
    );

  return (
    <article className="approval" aria-label={`${approval.tool} waiting for approval`}>
      <div className="approval-head">
        <div className="row">
          <span className="approval-tool">{approval.tool}</span>
          <span className="muted small">
            {label(approval.effect)} · {approval.trust} · {callLabel(approval.invocation_id)}
          </span>
        </div>
        <span className="muted small">
          expires <Time value={approval.expires_at} />
        </span>
      </div>
      <p className="small" style={{ marginTop: 4 }}>
        {approval.reason}
      </p>
      {approval.depth > 0 ? (
        <p className="small muted" style={{ marginTop: 4 }}>
          Delegated: <LineagePath lineage={approval.lineage} />
          {approval.root_run_id ? (
            <>
              {" "}
              · <Link to={`/runs/${approval.root_run_id}`}>open the orchestration</Link>
            </>
          ) : null}
        </p>
      ) : null}
      {showRun ? (
        <p className="small muted" style={{ marginTop: 4 }}>
          In run{" "}
          <Link className="mono" to={`/runs/${approval.run_id}`}>
            {shortId(approval.run_id)}
          </Link>
          {approval.agent ? ` (${approval.agent})` : ""}: “{approval.run_input}”
        </p>
      ) : null}
      <div className="args">
        <Arguments value={approval.arguments} />
      </div>
      {allowed ? (
        <div className="approval-actions">
          <label htmlFor={`reason-${approval.binding}`} className="visually-hidden">
            Reason (optional)
          </label>
          <input
            id={`reason-${approval.binding}`}
            type="text"
            placeholder="Reason (optional, recorded)"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
          <button type="button" className="btn primary" disabled={decide.isPending} onClick={() => act(true)}>
            <Icon name="check" size={14} /> Approve
          </button>
          <button type="button" className="btn danger" disabled={decide.isPending} onClick={() => act(false)}>
            <Icon name="close" size={14} /> Deny
          </button>
        </div>
      ) : (
        <p className="small muted">
          {me?.name ?? "You"} cannot approve tool calls. Someone with the approve permission can.
        </p>
      )}
    </article>
  );
}

export function DemoNotice() {
  const deployments = useDeployments();
  const real = deployments.data?.filter((d) => !d.builtin && d.tasks.includes("text_generation")) ?? [];
  if (!deployments.data || real.length > 0 || !deployments.data.some((d) => d.id === "demo")) return null;
  return (
    <div className="notice" role="note">
      <div>
        <p>
          <strong>Runs use the built-in demo model.</strong> It is deterministic rules, not a language model: it calls
          tools for simple requests (“list files”, “write notes.txt: …”, questions about your documents) so you can try
          tools, approvals, memory and knowledge without credentials. Every answer it gives starts with “[demo]”.
        </p>
        <p>
          <Link to="/models">Add a model deployment</Link> and point an agent at it for real answers.
        </p>
      </div>
    </div>
  );
}

export function usage(run: RunSummary): string {
  return `${count(run.turns, "turn")} · ${count(run.tool_calls, "tool call")} · ${run.usage.input_tokens.toLocaleString()} in / ${run.usage.output_tokens.toLocaleString()} out`;
}
