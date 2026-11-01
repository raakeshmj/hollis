# M7 plan: multi-agent orchestration and collaboration

Branch `m7-orchestration`, from `m6-product`. Owned end to end. No separate approval step.

## Starting point

M6 gives a complete single-agent product:
- durable runs (event log, fold, recovery), the gateway, tools with policy and approvals, context, memory, knowledge;
- agents as named configurations (`AgentInput`) that operators run;
- one platform layer behind the REST API, SDK, CLI and web app.

What's missing is any way for one agent to hand bounded work to another and use the result. The research ([docs/research/multi-agent.md](../research/multi-agent.md)) is sobering:
- multi-agent systems fail often, and mostly for structural reasons: specification, inter-agent misalignment, missing verification (MAST);
- they help on parallelizable, breadth-first work and hurt on sequential work (Kim et al. 2025; Anthropic's research system);
- independent agents amplify errors unless a coordinator checks their output.

The design therefore optimizes for **bounded, verifiable delegation**, not for agents talking freely.

## Architecture

```
 parent run (an agent)                         child run (another agent)
 ─────────────────────                         ─────────────────────────
 model turn: tool call delegate_researcher ─┐
 ToolCallDecided (policy; approval if the   │
   delegated agent can change things)       │  RunCreated(lineage, assignment, delegation limits,
 ToolExecutionStarted ──────────────────────┼─▶   narrowed tools/memory/knowledge)   id = uuid5(parent, invocation)
   (the delegate tool waits) ◀───────────────┼── ... a normal run: tools, approvals, retries ...
 ToolResultRecorded(report: outcome from the│  RunCompleted / RunFailed / RunCancelled
   child's log, verified evidence, framed   │
   untrusted answer)                        ┘
 next model turn reads the report
```

- **A delegation is a tool call whose execution is a child run.** Policy, approval, recorded intent and result, read-only parallelism and recovery all come from the existing tool lifecycle (ADR-0014). Child runs are ordinary runs: the same runner, events, inspector, approvals and cancellation. There is no second execution engine.
- **Deterministic child identity.** The child's run id is derived from (parent run, invocation). Re-executing the invocation after a crash attaches to the existing child instead of creating another: the store refuses a second `RunCreated` for the same id. That makes the delegate tool truly idempotent, so recovery re-executes it safely.
- **Lineage is recorded in the child's `RunCreated`** (v6): root, parent, invocation, depth, plus the structured assignment and the delegation limits that apply below it. The fold checks depth against the limits. The run index derives parent, root and depth columns from it, so the tree is rebuildable from the logs.
- **Structured collaboration:**
  - down: an `Assignment` (agent, objective, context, expected output, prior results passed by reference);
  - up: a report built by Hollis from the child's log (outcome, usage, verification, side effects with unknown outcome, evidence), plus the child's self-reported status, confidence and open questions, and its answer framed as untrusted;
  - evidence crosses runs only as Hollis-rendered blocks from the child's *recorded* trusted sources, keyed by stable `C-` handles, so the parent's grounding check can verify citations and a child cannot forge evidence.
- **Two ways to coordinate, one mechanism:**
  - *Model-driven:* an agent with `delegates` gets one `delegate_<agent>` tool per delegate. Independent read-only delegations in one turn run concurrently; dependent work happens in later turns, passing earlier results by reference (`inputs`). Hierarchy is allowed within a depth limit.
  - *Developer-defined plans:* a DAG of steps run by the built-in deterministic `workflow` coordinator. Like the demo model, it's not a language model. It issues each wave of ready steps as delegate calls and aggregates the results, comparing grouped answers and keeping disagreements. A root run like any other; it costs no coordinator tokens.
- **Single agent stays the default.** Nothing changes for agents without delegates.

## Security and limits

- **Permissions only narrow.**
  - A child's memory and knowledge are its agent's configuration ∩ the parent run's effective access ∩ the run owner's current grants.
  - Its tool calls are decided by the same workspace policy, with the delegation depth as an additional fact rules can match.
  - Its approvals need the approve permission and access to the child run, which belongs to the same owner as the root.
  - Delegating to an agent mutates nothing by itself. Its tool effect is the strongest effect among the delegate's tools (transitively), so the default policy lets read-only delegations run and requires approval for delegations that can change things.
- **Live authorization:** each delegation re-checks that the run's owner still exists and may run agents. Result references (`inputs`) resolve only to finished delegations of the same parent; an identifier alone grants nothing.
- **Bounded:**
  - depth (default 2, absolute 5);
  - children per run;
  - runs per tree;
  - an optional tree-wide token budget, enforced before every model call by a runner guard and at every delegation;
  - per-run `max_total_tokens` (fold-enforced);
  - a wait limit within the parent's deadline.
  
  Limits are recorded and inherited, never widened. Self-delegation and cycles are possible and bounded.
- **Failures are explicit:**
  - a failed, cancelled, timed-out or budget-stopped child becomes a report the parent can act on;
  - a child whose execution stopped unexpectedly is cancelled, so no orphans;
  - cancelling a run cancels its descendants;
  - ambiguous side effects in a child surface in the parent's report;
  - no exactly-once claims.

## Interfaces

- **REST:**
  - `GET /v1/runs/{id}/tree` (the orchestration view);
  - lineage on run summaries;
  - `roots` filter on run lists;
  - `plan` on `POST /v1/runs`;
  - `delegates` and `delegation` on agents;
  - lineage on approvals.
- **SDK:** `runs.start(..., plan=...)`, `Run.tree()`, `Plan`/`Step`, `runs.list(roots=...)`.
- **CLI:** `hollis runs tree`, `hollis runs start --plan`, `hollis agents create --delegate`.
- **Web:**
  - the run inspector gains an orchestration panel: tree, timeline, dependencies, usage, verification, failures and approvals per agent;
  - lineage breadcrumbs;
  - runs list grouped by root;
  - agent editor delegates and limits.
- **A2A:** a recorded position (ADR-0032), with a mapping from task to run and from Assignment/report to Message/Artifact. The internal model stays independent of it. Implementation is deferred until it can be conformance-tested.

## Evaluation

Offline, reproducible, with deterministic simulated agents (not language models) on the real platform. Scenarios:
- independent breadth;
- dependent pipeline;
- conflicting sources;
- agent failure;
- interrupted execution;
- permission boundary;
- budget exhaustion;
- runaway recursion.

Each is compared against single-agent baselines at equal per-run budget and at equal total budget. Metrics: success, correctness, evidence quality, completeness, latency, tokens, coordination overhead, recovery and security. Optional live evaluation against a configured deployment (opt-in, not run here).

## Commits

1. docs: plan and research note.
2. feat(runtime): lineage, assignment and delegation limits in `RunCreated` v6; `max_total_tokens`; model-call guard; caller-chosen run ids; delegation depth in policy.
3. feat(platform): orchestration (delegate tools, child runs, narrowing, limits, reports, cascade, tree views), workflow coordinator, demo delegation.
4. feat(server,sdk,cli): orchestration in every interface.
5. feat(web): orchestration inspector.
6. test(evals): orchestration evaluation and report.
7. docs: ADRs 0030–0032, guides, references, examples, roadmap, requirements.
