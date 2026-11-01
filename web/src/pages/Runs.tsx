import { useState } from "react";
import { useAgents, useRuns } from "../api/hooks";
import type { RunPhase } from "../api/types";
import { Composer, RunTable } from "../components/runs";
import { Async, Empty, PageHead, Pager } from "../components/ui";
import { label } from "../lib/format";

const FILTERS: (RunPhase | "all")[] = ["all", "running", "waiting_approval", "completed", "failed", "cancelled", "interrupted"];
const LIMIT = 25;

export function Runs() {
  const [phase, setPhase] = useState<RunPhase | "all">("all");
  const [agent, setAgent] = useState("");
  const [offset, setOffset] = useState(0);
  const [delegated, setDelegated] = useState(false);
  const agents = useAgents();
  const runs = useRuns({
    phase: phase === "all" ? undefined : phase,
    agent: agent || undefined,
    roots: delegated ? undefined : true,
    limit: LIMIT,
    offset,
  });

  return (
    <>
      <PageHead
        title="Runs"
        description="Every run executes in the server: it keeps going when you close this page, waits for approvals, and resumes after a restart."
      />
      <Composer />
      <div className="toolbar" style={{ marginTop: 24 }}>
        <div className="segmented" role="group" aria-label="Filter by phase">
          {FILTERS.map((value) => (
            <button
              key={value}
              type="button"
              aria-pressed={phase === value}
              onClick={() => {
                setPhase(value);
                setOffset(0);
              }}
            >
              {value === "all" ? "All" : label(value)}
            </button>
          ))}
        </div>
        <label className="row small">
          Agent
          <select
            value={agent}
            onChange={(event) => {
              setAgent(event.target.value);
              setOffset(0);
            }}
            style={{ width: "auto", minHeight: 28, padding: "2px 8px" }}
          >
            <option value="">all</option>
            {agents.data?.map((a) => (
              <option key={a.name}>{a.name}</option>
            ))}
          </select>
        </label>
        <label className="check small">
          <input
            type="checkbox"
            checked={delegated}
            onChange={(event) => {
              setDelegated(event.target.checked);
              setOffset(0);
            }}
          />
          <span>Include delegated runs</span>
        </label>
      </div>
      <Async
        query={runs}
        empty={(page) =>
          page.items.length === 0 ? (
            <Empty title={phase === "all" && !agent ? "No runs yet" : "No runs match"}>
              {phase === "all" && !agent ? "Start one above." : "Try another filter."}
            </Empty>
          ) : null
        }
      >
        {(page) => (
          <>
            <RunTable runs={page.items} />
            <Pager offset={offset} limit={LIMIT} total={page.total} onChange={setOffset} />
          </>
        )}
      </Async>
    </>
  );
}
