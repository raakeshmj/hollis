import { Link } from "react-router";
import { useAgents, useCreateStarter } from "../api/hooks";
import { Icon } from "../components/Icon";
import { Async, Empty, PageHead, Tags, Time } from "../components/ui";
import { isAdmin, useMe } from "../lib/session";

export function Agents() {
  const agents = useAgents();
  const starter = useCreateStarter();
  const me = useMe();
  const admin = isAdmin(me);
  return (
    <>
      <PageHead
        title="Agents"
        description="An agent is a model with instructions, the tools it may use, and the memory and knowledge it may reach. Runs use the caller’s access intersected with the agent’s."
        actions={
          admin ? (
            <Link className="btn primary" to="/agents/new">
              <Icon name="plus" size={14} /> New agent
            </Link>
          ) : null
        }
      />
      <Async
        query={agents}
        empty={(items) =>
          items.length === 0 ? (
            <Empty
              title="No agents yet"
              actions={
                admin ? (
                  <>
                    <button type="button" className="btn primary" disabled={starter.isPending} onClick={() => starter.mutate()}>
                      Create the starter agent
                    </button>
                    <Link className="btn" to="/agents/new">
                      Define one from scratch
                    </Link>
                  </>
                ) : null
              }
            >
              {admin ? "The starter agent uses the demo model and the workspace’s built-in tools." : "An admin defines agents."}
            </Empty>
          ) : null
        }
      >
        {(items) => (
          <div className="table-wrap" role="region" aria-label="Agents" tabIndex={0}>
            <table>
              <thead>
                <tr>
                  <th scope="col">Agent</th>
                  <th scope="col">Model</th>
                  <th scope="col">Tools</th>
                  <th scope="col" className="hide-narrow">
                    Memory
                  </th>
                  <th scope="col" className="hide-narrow">
                    Knowledge
                  </th>
                  <th scope="col">Updated</th>
                </tr>
              </thead>
              <tbody>
                {items.map((agent) => (
                  <tr key={agent.name}>
                    <td>
                      <Link className="row-link" to={`/agents/${agent.name}`}>
                        {agent.name}
                      </Link>
                      {agent.description ? <div className="small muted">{agent.description}</div> : null}
                    </td>
                    <td className="mono small">{agent.model}</td>
                    <td>
                      {agent.tools.length}
                      {agent.delegates.length ? (
                        <div className="small muted">delegates to {agent.delegates.join(", ")}</div>
                      ) : null}
                    </td>
                    <td className="hide-narrow">
                      <Tags values={agent.memory?.write.length ? agent.memory.write : (agent.memory?.read ?? [])} />
                    </td>
                    <td className="hide-narrow">
                      <Tags values={agent.knowledge?.collections ?? []} />
                    </td>
                    <td className="muted nowrap">
                      <Time value={agent.updated_at} /> · v{agent.version}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Async>
    </>
  );
}
