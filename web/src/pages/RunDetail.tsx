import { Fragment, useState } from "react";
import { Link, useParams } from "react-router";
import { ApiError } from "../api/client";
import { useApprovals, useArtifact, useRun, useRunAction, useRunEvents, useRunStream } from "../api/hooks";
import type { RunDetail as Detail, ToolCallView, TurnView } from "../api/types";
import { Icon } from "../components/Icon";
import { ApprovalItem, usage } from "../components/runs";
import {
  Arguments,
  Confirm,
  CopyButton,
  Empty,
  ErrorState,
  Loading,
  PageHead,
  Phase,
  Tags,
  Time,
  errorText,
  useToast,
} from "../components/ui";
import { callLabel, duration, label, shortId, time, tokens } from "../lib/format";
import { isAdmin, useMe } from "../lib/session";

const CITATION = /(\[(?:S\d{1,3}|C-[0-9a-f]{8})\])/;

/** Text with citation keys marked (React escapes everything; nothing is rendered as HTML). */
function Cited({ text }: { text: string }) {
  return (
    <>
      {text.split(CITATION).map((part, i) =>
        CITATION.test(part) ? (
          <span className="cite" key={i}>
            {part.slice(1, -1)}
          </span>
        ) : (
          <Fragment key={i}>{part}</Fragment>
        ),
      )}
    </>
  );
}

const TERMINAL = new Set(["completed", "failed", "cancelled"]);

export function RunDetail() {
  const { id = "" } = useParams();
  const run = useRun(id);
  const [tab, setTab] = useState<"timeline" | "events">("timeline");
  const active = run.data ? !TERMINAL.has(run.data.summary.phase) : false;
  useRunStream(id, active, () => undefined);

  if (run.isPending) return <Loading rows={6} label="Loading the run" />;
  if (run.error) {
    if (run.error instanceof ApiError && run.error.status === 404)
      return (
        <>
          <PageHead title="Run not found" crumb={<Link to="/runs">Runs</Link>} />
          <Empty title="This run does not exist, or it is not visible to you." />
        </>
      );
    return <ErrorState error={run.error} retry={() => void run.refetch()} />;
  }
  const detail = run.data;
  return (
    <>
      <RunHeader detail={detail} live={active} />
      <blockquote className="input-quote" aria-label="Task">
        {detail.input}
      </blockquote>
      <div className="segmented" role="group" aria-label="View" style={{ marginBottom: 20 }}>
        {(["timeline", "events"] as const).map((value) => (
          <button
            key={value}
            type="button"
            aria-pressed={tab === value}
            onClick={() => setTab(value)}
          >
            {value === "timeline" ? "Timeline" : "Event log"}
          </button>
        ))}
      </div>
      {tab === "timeline" ? (
        <div className="columns">
          <Timeline detail={detail} />
          <SidePanel detail={detail} />
        </div>
      ) : (
        <EventLog id={id} />
      )}
    </>
  );
}

function RunHeader({ detail, live }: { detail: Detail; live: boolean }) {
  const s = detail.summary;
  const me = useMe();
  const action = useRunAction();
  const toast = useToast();
  const [confirm, setConfirm] = useState(false);
  const mine = isAdmin(me) || s.created_by === me?.name;
  const cancellable = mine && !TERMINAL.has(s.phase);
  const resumable = mine && s.phase === "interrupted";
  return (
    <>
      <PageHead
        crumb={<Link to="/runs">Runs</Link>}
        title={
          <span className="row" style={{ gap: 12 }}>
            Run <span className="mono">{shortId(s.run_id)}</span>
          </span>
        }
        actions={
          <>
            <CopyButton value={s.run_id} label="Copy id" />
            {resumable ? (
              <button
                type="button"
                className="btn"
                disabled={action.isPending}
                onClick={() =>
                  action.mutate(
                    { id: s.run_id, action: "resume" },
                    { onError: (error) => toast(errorText(error), "error") },
                  )
                }
              >
                <Icon name="play" size={14} /> Resume
              </button>
            ) : null}
            {cancellable ? (
              <button type="button" className="btn danger" onClick={() => setConfirm(true)}>
                <Icon name="stop" size={14} /> Cancel run
              </button>
            ) : null}
          </>
        }
      />
      <div className="run-head" style={{ marginTop: -12, marginBottom: 20 }}>
        <Phase phase={s.phase} />
        {live ? <span className="live">live</span> : null}
        <span>{s.agent ? <Link to={`/agents/${s.agent}`}>{s.agent}</Link> : "ad hoc run"}</span>
        <span>
          {s.model}
          {s.protocol === "hollis-demo" ? " (deterministic rules, not a language model)" : ` · ${s.provider}`}
        </span>
        <span>
          started <Time value={s.created_at} /> by {s.created_by ?? "unknown"}
        </span>
        {s.session ? <span>session {s.session}</span> : null}
        <span>{duration(s.duration_s)}</span>
        <span>{usage(s)}</span>
      </div>
      {s.error && !detail.failure ? (
        <div className="notice danger" role="alert" style={{ marginBottom: 20 }}>
          <p>
            Execution stopped: {s.error}. The run is resumable from its event log.
          </p>
        </div>
      ) : null}
      <Confirm
        open={confirm}
        title="Cancel this run?"
        confirmLabel="Cancel run"
        danger
        busy={action.isPending}
        onCancel={() => setConfirm(false)}
        onConfirm={() =>
          action.mutate(
            { id: s.run_id, action: "cancel", reason: "cancelled in the web app" },
            {
              onSuccess: () => toast("Run cancelled."),
              onError: (error) => toast(errorText(error), "error"),
              onSettled: () => setConfirm(false),
            },
          )
        }
      >
        <p>
          The run stops for good. A tool call in progress is recorded as interrupted: its effects are unknown, so check
          them if it matters.
        </p>
      </Confirm>
    </>
  );
}

function Timeline({ detail }: { detail: Detail }) {
  const s = detail.summary;
  const approvals = useApprovals();
  const pending = approvals.data?.filter((a) => a.run_id === s.run_id) ?? [];
  const finished = TERMINAL.has(s.phase);
  return (
    <ol className="timeline" aria-label="Run timeline">
      <li className="start">
        <div className="step-title">
          <strong>Started</strong>
          <span className="when">{time(s.created_at)}</span>
        </div>
        <p className="step-meta">
          {detail.memories.length ? `${detail.memories.length} memories recalled · ` : ""}
          {detail.passages.length ? `${detail.passages.length} knowledge passages retrieved · ` : ""}
          {detail.tools.length ? `${detail.tools.length} tools offered` : "no tools"}
        </p>
      </li>
      {detail.compactions.map((c) => (
        <li key={`c${c.index}`}>
          <div className="step-title">
            <strong>Earlier turns summarized</strong>
          </div>
          <p className="step-meta">
            Turns 0–{c.through_turn - 1} condensed ({label(c.method)}, {c.items} items). The full record stays in the
            event log.
          </p>
        </li>
      ))}
      {detail.turns.map((turn) => (
        <Turn key={turn.index} turn={turn} runId={s.run_id} pending={pending} />
      ))}
      {finished ? (
        <li className={`end ${s.phase}`}>
          <div className="step-title">
            <strong>{s.phase === "completed" ? "Answer" : s.phase === "failed" ? "Failed" : "Cancelled"}</strong>
            <span className="when">{time(s.updated_at)}</span>
          </div>
          {detail.answer ? (
            <div className="answer">
              <Cited text={detail.answer} />
            </div>
          ) : null}
          {detail.failure ? (
            <div className="notice danger" style={{ marginTop: 8 }}>
              <p>
                <strong>{label(detail.failure.reason)}</strong>
                {detail.failure.kind ? ` (${label(detail.failure.kind)})` : ""}: {detail.failure.message}
              </p>
            </div>
          ) : null}
          {detail.cancellation ? (
            <p className="step-meta">
              Cancelled by {detail.cancellation.by}
              {detail.cancellation.reason ? `: ${detail.cancellation.reason}` : ""}.
              {detail.cancellation.interrupted.length
                ? ` Interrupted tool calls (effects unknown): ${detail.cancellation.interrupted.map(callLabel).join(", ")}.`
                : ""}
            </p>
          ) : null}
        </li>
      ) : (
        <li className="end open">
          <div className="step-title">
            <strong>{s.phase === "waiting_approval" ? "Waiting for a decision" : label(s.phase)}</strong>
          </div>
          <p className="step-meta">
            {s.phase === "waiting_approval"
              ? "The run continues once every pending tool call is approved or denied."
              : s.phase === "interrupted"
                ? "Nothing is executing this run. Resume it to continue from the event log."
                : "Executing in the server. This page updates as events arrive."}
          </p>
        </li>
      )}
    </ol>
  );
}

function Turn({ turn, runId, pending }: { turn: TurnView; runId: string; pending: { invocation_id: string }[] }) {
  const call = turn.model_call;
  const failed = call.attempts.filter((a) => a.outcome === "failed");
  return (
    <li className={turn.tool_calls.length ? "tool" : undefined}>
      <div className="step-title">
        <strong>Model call {turn.index + 1}</strong>
        <span className="step-meta">
          {call.stop_reason ? label(call.stop_reason) : "in flight"}
          {call.usage ? ` · ${tokens(call.usage.input_tokens)} in / ${tokens(call.usage.output_tokens)} out` : ""}
          {call.duration_ms !== null && call.duration_ms !== undefined ? ` · ${duration(call.duration_ms / 1000)}` : ""}
          {call.attempts.length > 1 ? ` · ${call.attempts.length} attempts` : ""}
        </span>
        <span className="when">{time(call.attempts[0]?.started_at)}</span>
      </div>
      {failed.map((a) => (
        <p key={a.attempt} className="step-meta" style={{ color: "var(--brass-text)" }}>
          Attempt {a.attempt} failed: {label(a.failure_kind ?? "unknown")}
          {a.message ? ` (${a.message})` : ""}
        </p>
      ))}
      {call.text && turn.tool_calls.length ? <p className="small" style={{ marginTop: 6 }}>{call.text}</p> : null}
      {turn.tool_calls.map((tool) => (
        <ToolCall
          key={tool.invocation_id}
          tool={tool}
          runId={runId}
          waiting={pending.some((p) => p.invocation_id === tool.invocation_id)}
        />
      ))}
    </li>
  );
}

function ToolCall({ tool, runId, waiting }: { tool: ToolCallView; runId: string; waiting: boolean }) {
  const approvals = useApprovals();
  const approval = approvals.data?.find((a) => a.invocation_id === tool.invocation_id);
  const [artifact, setArtifact] = useState<string | null>(null);
  const tone =
    tool.status === "succeeded"
      ? "var(--success)"
      : ["failed", "denied", "invalid", "timed_out", "ambiguous"].includes(tool.status)
        ? "var(--danger)"
        : "var(--brass-text)";
  return (
    <div className="tool-call">
      <div className="step-title">
        <span className="mono" style={{ fontWeight: 500 }}>
          {tool.tool}
        </span>
        <span className="small" style={{ color: tone }}>
          {label(tool.status)}
        </span>
        <span className="step-meta">
          {tool.decision ? `policy: ${label(tool.decision)}` : ""}
          {tool.rule ? ` (${tool.rule})` : ""}
          {tool.approved !== null && tool.approved !== undefined
            ? ` · ${tool.approved ? "approved" : "denied"} by ${tool.approver}${tool.approval_reason ? `: “${tool.approval_reason}”` : ""}`
            : ""}
          {tool.duration_ms !== null && tool.duration_ms !== undefined ? ` · ${duration(tool.duration_ms / 1000)}` : ""}
        </span>
      </div>
      {waiting && approval ? (
        <div className="step-body">
          <ApprovalItem approval={approval} showRun={false} />
        </div>
      ) : (
        <details className="step-body">
          <summary>Arguments{tool.result ? " and result" : ""}</summary>
          <Arguments value={tool.arguments ?? tool.raw_arguments} />
          {tool.result ? (
            <>
              <p className="step-meta" style={{ margin: "10px 0 6px" }}>
                Result ({tool.result_chars.toLocaleString()} characters
                {tool.result_truncated ? ", preview" : ""})
              </p>
              <pre className="code-block">{tool.result}</pre>
            </>
          ) : null}
          {tool.artifact_id ? (
            <p style={{ marginTop: 8 }}>
              <button type="button" className="btn small" onClick={() => setArtifact(tool.artifact_id ?? null)}>
                Open the full result (artifact)
              </button>
            </p>
          ) : null}
          {artifact ? <ArtifactView runId={runId} artifactId={artifact} /> : null}
        </details>
      )}
    </div>
  );
}

function ArtifactView({ runId, artifactId }: { runId: string; artifactId: string }) {
  const artifact = useArtifact(runId, artifactId);
  if (artifact.isPending) return <Loading rows={3} />;
  if (artifact.error) return <ErrorState error={artifact.error} />;
  return (
    <>
      <p className="step-meta" style={{ margin: "10px 0 6px" }}>
        Artifact {artifact.data.artifact_id} · {artifact.data.chars.toLocaleString()} characters · {artifact.data.source}
      </p>
      <pre className="code-block">{artifact.data.text}</pre>
    </>
  );
}

function SidePanel({ detail }: { detail: Detail }) {
  const contexts = detail.turns.map((t) => t.model_call.context).filter((c) => c);
  const last = contexts[contexts.length - 1];
  const g = detail.grounding;
  return (
    <aside className="side-panel" aria-label="Run context">
      {g ? (
        <div>
          <h3>Grounding</h3>
          <p className="small">
            {g.abstained
              ? "The answer declined to answer from the sources."
              : `${g.claims.filter((c) => c.support === "supported").length} of ${g.claims.length} claims supported by cited passages.`}
          </p>
          {g.fabricated.length ? (
            <p className="small" style={{ color: "var(--danger)" }}>
              Cited keys that were never retrieved: {g.fabricated.join(", ")}
            </p>
          ) : null}
          <p className="small muted">A lexical check, not a guarantee of correctness.</p>
        </div>
      ) : null}
      {detail.passages.length ? (
        <div>
          <h3>Knowledge passages</h3>
          {detail.passages.map((p) => (
            <div className="passage" key={p.chunk_id}>
              <span className="cite">{p.key}</span> <strong>{p.source_title}</strong>{" "}
              <span className="muted">{p.location}</span>
              <p>{p.text.length > 280 ? `${p.text.slice(0, 280)}…` : p.text}</p>
            </div>
          ))}
        </div>
      ) : null}
      {detail.memories.length ? (
        <div>
          <h3>Recalled memories</h3>
          {detail.memories.map((m) => (
            <div className="passage" key={m.memory_id}>
              <span className="tag">{m.scope}</span> <span className="muted small">{m.kind}</span>
              <p>{m.content}</p>
            </div>
          ))}
        </div>
      ) : null}
      <div>
        <h3>Access</h3>
        <dl className="facts small">
          <dt>Tools</dt>
          <dd>
            <Tags values={detail.tools} />
          </dd>
          <dt>Memory</dt>
          <dd>{detail.memory_access ? <Tags values={detail.memory_access.read} /> : <span className="muted">none</span>}</dd>
          <dt>Knowledge</dt>
          <dd>
            {detail.knowledge_access ? (
              <Tags values={detail.knowledge_access.collections} />
            ) : (
              <span className="muted">none</span>
            )}
          </dd>
          <dt>Budget</dt>
          <dd>{detail.budget.max_output_tokens.toLocaleString()} output tokens per call</dd>
        </dl>
      </div>
      {last ? (
        <div>
          <h3>Context</h3>
          <dl className="facts small">
            <dt>Last request</dt>
            <dd>
              ~{last.estimate.toLocaleString()} tokens ({label(last.accounting)})
            </dd>
            <dt>Budget</dt>
            <dd>{last.budget ? `${last.budget.toLocaleString()} of ${last.window?.toLocaleString() ?? "?"}` : "unknown"}</dd>
            {last.cleared ? (
              <>
                <dt>Cleared</dt>
                <dd>{last.cleared} older tool results</dd>
              </>
            ) : null}
          </dl>
        </div>
      ) : null}
    </aside>
  );
}

function EventLog({ id }: { id: string }) {
  const events = useRunEvents(id);
  const [open, setOpen] = useState<number | null>(null);
  if (events.isPending) return <Loading rows={6} />;
  if (events.error) return <ErrorState error={events.error} retry={() => void events.refetch()} />;
  return (
    <div className="table-wrap" role="region" aria-label="Event log" tabIndex={0}>
      <table className="event-log">
        <thead>
          <tr>
            <th scope="col" className="num">
              #
            </th>
            <th scope="col">Time</th>
            <th scope="col">Event</th>
            <th scope="col">Summary</th>
          </tr>
        </thead>
        <tbody>
          {events.data.map((event) => (
            <Fragment key={event.sequence}>
              <tr>
                <td className="num muted">{event.sequence}</td>
                <td className="nowrap muted">{time(event.occurred_at)}</td>
                <td className="nowrap">
                  <button
                    type="button"
                    className="btn small quiet"
                    aria-expanded={open === event.sequence}
                    onClick={() => setOpen(open === event.sequence ? null : event.sequence)}
                  >
                    {event.type}
                  </button>
                </td>
                <td>{event.summary}</td>
              </tr>
              {open === event.sequence ? (
                <tr>
                  <td />
                  <td colSpan={3}>
                    <pre className="code-block">{JSON.stringify(event.payload, null, 2)}</pre>
                  </td>
                </tr>
              ) : null}
            </Fragment>
          ))}
        </tbody>
      </table>
      <p className="small muted" style={{ marginTop: 12 }}>
        The event log is the run’s source of truth: every view on this page is computed from it.
      </p>
    </div>
  );
}

