# ADR-0032: A2A interoperability: a mapped boundary, implementation deferred

- Status: Accepted (M7)
- Date: 2026-11-01

## Context

The [Agent2Agent protocol](https://a2a-protocol.org/latest/specification/) (1.0) is the emerging standard for agents from different vendors to exchange work:
- **Discovery:** Agent Cards at `/.well-known/agent-card.json`.
- **Work:** Tasks with messages, parts and artifacts, through `SendMessage`, `GetTask`, `CancelTask` and `SubscribeToTask`.
- **Task states:** submitted, working, input-required, auth-required, completed, failed, canceled and rejected.
- **Security:** mandatory authentication.

M7 asks whether Hollis should use it, while keeping internal orchestration independent of it.

## Decision

**Internal orchestration does not use A2A.** Delegation between Hollis agents is durable, in-process and permission-narrowing ([ADR-0030](0030-orchestration-on-durable-runs.md), [ADR-0031](0031-orchestration-authority-and-limits.md)). A wire protocol would add serialization and a second identity model, and would lose the fold's guarantees: deterministic child ids, recorded lineage, evidence verification.

**The boundary is designed to map onto A2A:**

| A2A | Hollis |
|---|---|
| Agent Card, skills | an agent's name, description and the schema of its delegate tool; authentication from ADR-0028 |
| `SendMessage` creating a Task | starting a run of an agent with an assignment (taskId ↔ run id, contextId ↔ session or root run) |
| Task states | run phases: queued/running ↔ submitted/working, waiting_approval ↔ input-required, completed/failed/cancelled ↔ completed/failed/canceled; a refused delegation ↔ rejected |
| Message parts | the rendered assignment (text), and inputs as data parts |
| Artifacts | the delegation report: Hollis-observed facts (data part), the framed answer (text part) and the evidence |
| `SubscribeToTask` | the run's event stream (SSE) |
| `CancelTask` | `cancel_run`, with cascade |

**Implementation is deferred** until both of these are available:
1. a way to test conformance offline or against reference implementations;
2. a concrete interoperability need.

The likely first steps:
- **Server side:** expose selected agents as A2A skills, mapping tasks to runs; the authorization rules are unchanged.
- **Client side:** an external A2A agent as a delegate, through a tool marked `external`. Its results are untrusted and it gets approval by default, like MCP tools ([ADR-0017](0017-mcp-integration.md)).

## Alternatives considered

- **Implement A2A now from the specification.** Without a conformance suite or a peer to test against, Hollis could not honestly claim interoperability: the same rule as for model servers (CLAUDE.md, "no compatibility claims without a recorded conformance run"). The specification changed method names between 0.3 and 1.0.
- **Use A2A internally as well.** Rejected above: it trades durable, verifiable coordination for a transport.
- **Ignore A2A.** That would leave no path for cross-vendor agents. The mapping costs nothing now and keeps the internal model compatible.

## Trade-offs

- **Gains:** no unverified protocol surface, and no premature dependency. The internal model already has the shapes A2A needs: structured assignments, reports with artifacts, mapped task states.
- **Costs:** Hollis agents cannot yet be called by, or call, other vendors' agents.

## Consequences

- No code in M7.
- The mapping above constrains future changes to assignments, reports and run phases: keep them expressible in A2A terms.
- Revisit when a conformance suite or a concrete integration exists.
