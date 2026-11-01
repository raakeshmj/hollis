import { Link } from "react-router";
import { useAgents, useApprovals, useCreateStarter, useDeployments, useHealth, useRuns } from "../api/hooks";
import { ApprovalItem, Composer, DemoNotice, RunTable } from "../components/runs";
import { Async, Empty, PageHead, Section, StatusDot } from "../components/ui";
import { isAdmin, useMe } from "../lib/session";

export function Overview() {
  const me = useMe();
  const health = useHealth();
  const approvals = useApprovals();
  const runs = useRuns({ limit: 8 });
  const agents = useAgents();
  const deployments = useDeployments();
  const starter = useCreateStarter();
  const configured = deployments.data?.filter((d) => !d.builtin).length ?? 0;
  const h = health.data;
  const noAgents = agents.data?.length === 0;

  return (
    <>
      <PageHead
        title="Overview"
        description={h ? <span title={h.workspace_path}>Workspace {h.workspace} · Hollis {h.version}</span> : " "}
      />
      <DemoNotice />
      {h ? (
        <div className="strip" aria-label="Workspace status">
          <div>
            <span className="label">Health</span>
            <span className="value">
              <StatusDot tone={h.status === "ok" ? "ok" : "warn"}>{h.status === "ok" ? "Healthy" : "Needs attention"}</StatusDot>
            </span>
          </div>
          <div>
            <span className="label">Running now</span>
            <span className="value">{h.active_runs}</span>
          </div>
          <div>
            <span className="label">Waiting for approval</span>
            <span className="value">{h.pending_approvals}</span>
          </div>
          <div>
            <span className="label">Models</span>
            <span className="value">
              {configured ? `${configured} configured` : h.demo_model ? "demo only" : "none"}
              {h.deployment_problems ? <span className="small muted"> · {h.deployment_problems} need attention</span> : null}
            </span>
          </div>
        </div>
      ) : null}
      {h?.problems.length ? (
        <ul className="small" style={{ margin: "12px 0 0", paddingLeft: 18, color: "var(--brass-text)" }}>
          {h.problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      ) : null}

      <Section title="Start a run">
        {noAgents ? (
          <Empty
            title="No agents yet"
            actions={
              isAdmin(me) ? (
                <button type="button" className="btn primary" onClick={() => starter.mutate()} disabled={starter.isPending}>
                  Create the starter agent
                </button>
              ) : null
            }
          >
            An agent is a model with instructions, tools and access to memory and knowledge. The starter agent uses the
            demo model and the workspace’s built-in tools.
          </Empty>
        ) : (
          <Composer />
        )}
      </Section>

      {approvals.data && approvals.data.length > 0 ? (
        <Section title="Needs you" aside={<Link className="small" to="/approvals">All approvals</Link>}>
          {approvals.data.slice(0, 3).map((approval) => (
            <ApprovalItem key={approval.binding} approval={approval} />
          ))}
        </Section>
      ) : null}

      <Section title="Recent runs" aside={<Link className="small" to="/runs">All runs</Link>}>
        <Async
          query={runs}
          empty={(page) =>
            page.items.length === 0 ? (
              <Empty title="No runs yet">Start one above; it runs in the server and keeps going if you close this page.</Empty>
            ) : null
          }
        >
          {(page) => <RunTable runs={page.items} />}
        </Async>
      </Section>
    </>
  );
}
