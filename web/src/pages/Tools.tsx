import { Link } from "react-router";
import { useConnectMcp, useMcpServers, useToolActivity, useTools } from "../api/hooks";
import { Async, Empty, PageHead, Policy, Section, StatusDot, Tags, Time, errorText, useToast } from "../components/ui";
import { label, shortId } from "../lib/format";
import { isAdmin, useMe } from "../lib/session";

export function Tools() {
  const tools = useTools();
  const servers = useMcpServers();
  const activity = useToolActivity();
  const connect = useConnectMcp();
  const toast = useToast();
  const admin = isAdmin(useMe());
  return (
    <>
      <PageHead
        title="Tools and integrations"
        description="What agents can call, and what the policy decides before anything runs. Decisions use only facts Hollis controls: the tool’s declared effect and trust, and the environment, never descriptions or model output."
      />
      <Section title="Tools">
        <Async query={tools} empty={(items) => (items.length === 0 ? <Empty title="No tools configured" /> : null)}>
          {(items) => (
            <div className="table-wrap" role="region" aria-label="Tools" tabIndex={0}>
              <table>
                <thead>
                  <tr>
                    <th scope="col">Tool</th>
                    <th scope="col">Policy</th>
                    <th scope="col">Effect</th>
                    <th scope="col" className="hide-narrow">
                      Trust
                    </th>
                    <th scope="col" className="hide-narrow">
                      Runs as
                    </th>
                    <th scope="col">Source</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((tool) => (
                    <tr key={tool.name}>
                      <td>
                        <span className="mono" style={{ fontWeight: 500 }}>
                          {tool.name}
                        </span>
                        <div className="small muted">{tool.description.split("\n")[0]}</div>
                      </td>
                      <td>
                        <Policy decision={tool.policy} />
                        <div className="small muted">{tool.policy_rule ?? ""}</div>
                      </td>
                      <td className="small">{label(tool.effect)}</td>
                      <td className="small hide-narrow">{tool.trust}</td>
                      <td className="small hide-narrow" title="Isolation: process means no sandbox">
                        {tool.isolation === "process" ? "this process (not sandboxed)" : label(tool.isolation)}
                      </td>
                      <td className="small mono">{tool.source}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Async>
        <p className="small muted" style={{ marginTop: 12 }}>
          Policy rules live in hollis.toml (<code>[[policy.rules]]</code>); anything no rule matches falls back to the
          default: trusted read-only tools run, everything else waits for approval.
        </p>
      </Section>

      <Section title="MCP servers">
        <Async
          query={servers}
          empty={(items) =>
            items.length === 0 ? (
              <Empty title="No MCP servers configured">
                Add servers under <code>[[tools.mcp]]</code> in hollis.toml, with an allowlist of the tools agents may
                use. They connect when a run needs them.
              </Empty>
            ) : null
          }
        >
          {(items) => (
            <div className="table-wrap" role="region" aria-label="MCP servers" tabIndex={0}>
              <table>
                <thead>
                  <tr>
                    <th scope="col">Server</th>
                    <th scope="col">Status</th>
                    <th scope="col">Transport</th>
                    <th scope="col">Allowed tools</th>
                    <th scope="col">
                      <span className="visually-hidden">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((server) => (
                    <tr key={server.name}>
                      <td className="mono">{server.name}</td>
                      <td>
                        <StatusDot tone={server.status === "connected" ? "ok" : server.status === "error" ? "bad" : "off"}>
                          {label(server.status)}
                        </StatusDot>
                        {server.error ? <div className="small" style={{ color: "var(--danger)" }}>{server.error}</div> : null}
                      </td>
                      <td className="small">
                        {server.transport} <span className="muted mono">{server.target}</span>
                      </td>
                      <td>
                        <Tags values={server.status === "connected" ? server.tools : server.allowlist} />
                      </td>
                      <td>
                        {admin ? (
                          <button
                            type="button"
                            className="btn small"
                            disabled={connect.isPending}
                            onClick={() =>
                              connect.mutate(server.name, {
                                onSuccess: (view) =>
                                  toast(view.status === "connected" ? `Connected to ${view.name}.` : `${view.name}: ${view.error}`, view.status === "connected" ? "info" : "error"),
                                onError: (error) => toast(errorText(error), "error"),
                              })
                            }
                          >
                            {server.status === "connected" ? "Reconnect" : "Connect"}
                          </button>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Async>
      </Section>

      <Section title="Recent tool calls">
        <Async
          query={activity}
          empty={(items) => (items.length === 0 ? <p className="muted">No tool calls yet in runs you can see.</p> : null)}
        >
          {(items) => (
            <div className="table-wrap" role="region" aria-label="Recent tool calls" tabIndex={0}>
              <table>
                <thead>
                  <tr>
                    <th scope="col">When</th>
                    <th scope="col">Tool</th>
                    <th scope="col">Decision</th>
                    <th scope="col">Outcome</th>
                    <th scope="col">Approver</th>
                    <th scope="col">Run</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((a) => (
                    <tr key={a.invocation_id}>
                      <td className="muted nowrap">
                        <Time value={a.at} />
                      </td>
                      <td className="mono">{a.tool}</td>
                      <td>{a.decision ? <Policy decision={a.decision} /> : "-"}</td>
                      <td className="small">{a.outcome ? label(a.outcome) : <span className="muted">pending</span>}</td>
                      <td className="small">{a.approver ?? <span className="muted">-</span>}</td>
                      <td>
                        <Link className="mono" to={`/runs/${a.run_id}`}>
                          {shortId(a.run_id)}
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Async>
      </Section>
    </>
  );
}
