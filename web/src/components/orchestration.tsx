/** The orchestration inspector: who delegated what to whom, how it went, and what it cost.
 * Everything shown comes from the server's orchestration view (computed from the runs' event
 * logs); this file only lays it out. Agents' answers and self-reports are model output, and are
 * labelled as such. */

import { Fragment } from "react";
import { Link } from "react-router";
import type {
  Assignment,
  LineageStep,
  OrchestrationNode,
  OrchestrationView,
  ReportView,
  RunSummary,
} from "../api/types";
import { TERMINAL } from "../api/types";
import { count, duration, label, runner, shortId, tokens } from "../lib/format";
import { Phase } from "./ui";

/** Text-safe brand tones (each meets 4.5:1 on the sunken surface in both themes). Not the
 * danger colour: an identity must not read as an error. */
const TONES = ["var(--brass-text)", "var(--info)", "var(--success)", "var(--ink-2)"];

function tone(name: string): string {
  let hash = 0;
  for (const char of name) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return TONES[hash % TONES.length] ?? "var(--ink-2)";
}

/** A small monogram for an agent, with a stable colour from its name. Decorative: the name is
 * always written next to it. */
export function AgentBadge({ name }: { name: string }) {
  const letters = name
    .split("-")
    .map((part) => part[0] ?? "")
    .join("")
    .slice(0, 2)
    .toUpperCase();
  return (
    <span className="agent-badge" style={{ color: tone(name), borderColor: tone(name) }} aria-hidden="true">
      {letters || "?"}
    </span>
  );
}

const nodeName = (run: RunSummary) => runner(run) ?? "run";

/** Root › … › this run, each a link. */
export function LineageCrumbs({ lineage, current }: { lineage: readonly LineageStep[]; current: RunSummary }) {
  if (!lineage.length) return null;
  return (
    <nav className="lineage" aria-label="Delegated by">
      <span className="muted small">Delegated by</span>
      <ol>
        {lineage.map((step) => (
          <li key={step.run_id}>
            <Link to={`/runs/${step.run_id}`}>
              {step.agent ?? "run"} <span className="mono muted">{shortId(step.run_id)}</span>
            </Link>
          </li>
        ))}
        <li aria-current="page">
          {nodeName(current)} <span className="mono muted">{shortId(current.run_id)}</span>
        </li>
      </ol>
    </nav>
  );
}

/** The structured task a delegated run was given. */
export function AssignmentCard({ assignment }: { assignment: Assignment }) {
  return (
    <section className="assignment" aria-label="Assignment">
      <div className="row">
        <AgentBadge name={assignment.agent} />
        <strong>Assignment for {assignment.agent}</strong>
      </div>
      <dl className="facts small" style={{ marginTop: 8 }}>
        <dt>Objective</dt>
        <dd>{assignment.objective}</dd>
        {assignment.expected_output ? (
          <>
            <dt>Expected output</dt>
            <dd>{assignment.expected_output}</dd>
          </>
        ) : null}
        {assignment.context ? (
          <>
            <dt>Context</dt>
            <dd style={{ whiteSpace: "pre-wrap" }}>{assignment.context}</dd>
          </>
        ) : null}
        {assignment.inputs.length ? (
          <>
            <dt>Results passed in</dt>
            <dd>
              {assignment.inputs.map((input) => (
                <Link key={input.run_id} className="tag" to={`/runs/${input.run_id}`}>
                  {input.agent ?? "run"} {shortId(input.run_id)}
                </Link>
              ))}
            </dd>
          </>
        ) : null}
        {assignment.evidence.length ? (
          <>
            <dt>Evidence handed down</dt>
            <dd>{count(assignment.evidence.length, "passage")} (recorded, verifiable)</dd>
          </>
        ) : null}
      </dl>
    </section>
  );
}

/** The whole orchestration: totals, tree, timeline, refusals and disagreements. */
export function OrchestrationPanel({ tree, focus }: { tree: OrchestrationView; focus: string }) {
  const names = new Map(tree.nodes.map((n) => [n.run.run_id, nodeName(n.run)]));
  return (
    <div className="orchestration">
      <Totals tree={tree} />
      <ol className="orch-tree" aria-label="Runs in this orchestration">
        {tree.nodes.map((node) => (
          <Node key={node.run.run_id} node={node} focus={focus} names={names} />
        ))}
      </ol>
      <Gantt tree={tree} names={names} />
      {tree.refusals.length ? (
        <div className="orch-list">
          <h3>Refused delegations</h3>
          <ul>
            {tree.refusals.map((r, i) => (
              <li key={i} className="small">
                <strong>{r.agent}</strong>, asked by{" "}
                <Link to={`/runs/${r.parent_run_id}`}>{names.get(r.parent_run_id) ?? shortId(r.parent_run_id)}</Link>: {r.reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {tree.disagreements.length ? (
        <div className="orch-list">
          <h3>Disagreements</h3>
          <p className="small muted">Kept as reported, not resolved: agents asked the same thing answered differently.</p>
          {tree.disagreements.map((d, i) => (
            <div key={i} className="disagreement">
              <p className="small">
                <strong>{d.objective}</strong>
              </p>
              <ul>
                {d.runs.map((run, j) => (
                  <li key={run} className="small">
                    <Link to={`/runs/${run}`}>{names.get(run) ?? shortId(run)}</Link>: “{d.answers[j]}”
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      ) : null}
      {tree.hidden ? (
        <p className="small muted">{count(tree.hidden, "run")} in this orchestration are not visible to you.</p>
      ) : null}
    </div>
  );
}

function Totals({ tree }: { tree: OrchestrationView }) {
  const t = tree.totals;
  const limits = tree.limits;
  return (
    <dl className="orch-totals">
      <div>
        <dt>Runs</dt>
        <dd>
          {t.runs} <span className="muted small">· depth {t.max_depth}</span>
        </dd>
      </div>
      <div>
        <dt>Tokens</dt>
        <dd>
          {tokens(t.input_tokens + t.output_tokens)}{" "}
          <span className="muted small">
            · coordination {tokens(t.coordinator_tokens)}, work {tokens(t.worker_tokens)}
          </span>
        </dd>
      </div>
      <div>
        <dt>Time</dt>
        <dd>
          {duration(t.wall_s)} wall <span className="muted small">· {duration(t.agent_s)} of delegated work</span>
        </dd>
      </div>
      <div>
        <dt>Attention</dt>
        <dd>
          {t.waiting_approvals ? `${count(t.waiting_approvals, "approval")} waiting` : "no approvals waiting"}
          {t.failed ? <span style={{ color: "var(--danger)" }}> · {t.failed} failed</span> : null}
        </dd>
      </div>
      {limits ? (
        <div>
          <dt>Limits</dt>
          <dd className="small">
            depth {limits.max_depth} · {limits.max_children} per run · {limits.max_runs} runs
            {limits.token_budget ? ` · ${tokens(limits.token_budget)} token budget` : ""}
          </dd>
        </div>
      ) : null}
    </dl>
  );
}

function Verification({ report }: { report: ReportView }) {
  const v = report.verification;
  if (!v || !v.claims) return null;
  return (
    <span>
      {v.supported}/{v.claims} supported
      {v.fabricated ? <span style={{ color: "var(--danger)" }}> · {v.fabricated} fabricated</span> : null}
    </span>
  );
}

function Node({ node, focus, names }: { node: OrchestrationNode; focus: string; names: Map<string, string> }) {
  const run = node.run;
  const name = nodeName(run);
  const report = node.report;
  const failedReport = report && report.outcome !== "completed" && report.outcome !== "refused";
  const isFocus = run.run_id === focus;
  return (
    <li className={`orch-node${isFocus ? " focus" : ""}`} style={{ marginLeft: `${run.depth * 24}px` }}>
      <div className="orch-node-head">
        <AgentBadge name={name} />
        {isFocus ? (
          <strong aria-current="true">{name}</strong>
        ) : (
          <Link className="row-link" to={`/runs/${run.run_id}`}>
            {name}
          </Link>
        )}
        <span className="mono muted small">{shortId(run.run_id)}</span>
        <Phase phase={run.phase} />
        <span className="muted small">
          {tokens(run.usage.input_tokens + run.usage.output_tokens)} tokens · {duration(run.duration_s)}
        </span>
        {report ? (
          <span className="muted small">
            <Verification report={report} />
          </span>
        ) : null}
      </div>
      {run.objective ? <p className="orch-objective">{run.objective}</p> : null}
      <div className="orch-meta small">
        {node.inputs.length ? (
          <span>
            uses results of{" "}
            {node.inputs.map((id) => (
              <Link key={id} className="tag" to={`/runs/${id}`}>
                {names.get(id) ?? shortId(id)}
              </Link>
            ))}
          </span>
        ) : null}
        {report?.status || report?.confidence ? (
          <span className="muted">
            self-reported: {report.status ? label(report.status) : "no status"}
            {report.confidence ? `, ${report.confidence} confidence` : ""}
          </span>
        ) : null}
        {node.waiting_on ? <span style={{ color: "var(--brass-text)" }}>waiting on {node.waiting_on}</span> : null}
        {node.narrowed.length ? <span className="muted">not granted: {node.narrowed.join(", ")}</span> : null}
      </div>
      {report?.output ? (
        <p className="small">
          <span className={`output-status ${report.output.status}`}>
            {report.output.contract}: {report.output.status}
          </span>{" "}
          <span className="muted">
            {report.output.status === "accepted"
              ? "checked against its run’s log"
              : report.output.status === "rejected"
                ? `after ${report.output.attempts} attempts`
                : "no structured result"}
          </span>
        </p>
      ) : null}
      {report?.answer ? (
        <p className="small orch-answer">
          <span className="muted">Reported answer (unverified):</span> {report.answer}
        </p>
      ) : null}
      {report?.side_effects_unknown.length ? (
        <p className="small" style={{ color: "var(--danger)" }}>
          Unknown side effects (check before retrying): {report.side_effects_unknown.join(", ")}
        </p>
      ) : null}
      {failedReport && report.reason ? (
        <p className="small" style={{ color: "var(--danger)" }}>
          {label(report.outcome)}: {report.reason}
        </p>
      ) : null}
      {report?.open_questions.length ? (
        <p className="small muted">Open questions: {report.open_questions.join(" · ")}</p>
      ) : null}
    </li>
  );
}

/** One bar per run over the root's span, so overlapping (parallel) work is visible. */
function Gantt({ tree, names }: { tree: OrchestrationView; names: Map<string, string> }) {
  // Runs still going end at their last recorded event (the server's duration), not "now", so
  // the drawing depends only on the data.
  const spans = tree.nodes.map((node) => {
    const start = new Date(node.started_at).getTime();
    const end = node.finished_at ? new Date(node.finished_at).getTime() : start + node.run.duration_s * 1000;
    return { node, start, end: Math.max(end, start) };
  });
  const first = Math.min(...spans.map((s) => s.start));
  const last = Math.max(...spans.map((s) => s.end), first + 1);
  const total = last - first;
  return (
    <div className="gantt">
      <h3>Timeline</h3>
      <ol aria-label="When each run ran">
        {spans.map(({ node, start, end }) => {
          const offset = (start - first) / 1000;
          const length = (end - start) / 1000;
          const name = names.get(node.run.run_id) ?? "run";
          const running = !node.finished_at && !TERMINAL.includes(node.run.phase);
          return (
            <li key={node.run.run_id}>
              <span className="gantt-label" style={{ paddingLeft: `${node.run.depth * 12}px` }}>
                {name}
              </span>
              <span className="gantt-track" aria-hidden="true">
                <span
                  className={`gantt-bar ${node.run.phase}`}
                  style={{
                    left: `${((start - first) / total) * 100}%`,
                    width: `${Math.max(((end - start) / total) * 100, 0.8)}%`,
                  }}
                />
              </span>
              <span className="visually-hidden">
                {name}: started {duration(offset) || "0 ms"} in, {running ? "still running, so far" : "took"}{" "}
                {duration(length) || "0 ms"}
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/** "lead › writer" for an approval in a delegated run. */
export function LineagePath({ lineage }: { lineage: readonly string[] }) {
  return (
    <span className="lineage-path">
      {lineage.map((agent, i) => (
        <Fragment key={i}>
          {i ? " › " : null}
          {agent}
        </Fragment>
      ))}
    </span>
  );
}
