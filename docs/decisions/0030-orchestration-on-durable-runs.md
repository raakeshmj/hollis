# ADR-0030: Multi-agent orchestration as delegation to durable child runs

- Status: Accepted (M7)
- Date: 2026-11-01

## Context

FR-12 asks for several agents on one task, each with its own tools, model, memory and knowledge. The evidence ([research note](../research/multi-agent.md)) shows that:
- multi-agent systems fail mostly from structure: vague specification, misalignment between agents, and missing verification;
- they help on parallel, breadth-first work and hurt on sequential work;
- uncoordinated agents amplify errors.

Hollis already has durable runs, a tool lifecycle with policy and approvals, crash recovery, and views computed from event logs. Whatever coordinates agents must keep those properties (NFR-1, NFR-2, NFR-9) and must not become a second execution engine.

## Decision

**A delegation is a tool call whose execution is a child run.** An agent with `delegates` gets one `delegate_<agent>` tool per delegate.
- **Lifecycle:** calling the tool goes through the normal lifecycle: decision, approval if required, recorded intent, result.
- **Execution:** creates a run of the delegate agent and waits for it.
- **Concurrency:** read-only delegations in one turn run concurrently through the runtime's existing parallel execution; others run one at a time.
- **Children are ordinary runs:** same runner, events, retries, approvals, cancellation and inspector.

**Deterministic child identity.** The child's run id is derived from (parent run, invocation id). If the process stops mid-delegation, recovery re-executes the invocation, and it *attaches* to the existing child: the store refuses a second `RunCreated` for the same id. This is what makes the delegate tool genuinely idempotent ([ADR-0014](0014-tool-runtime-and-durable-lifecycle.md)), so re-execution is allowed. A child is never created twice.

**Lineage is recorded, not inferred.** The child's `RunCreated` (schema v6, upcast from v5) records:
- `lineage`: root, parent, invocation, depth;
- the structured `assignment`: agent, objective, context, expected output, inputs and evidence handed down;
- the `delegation` limits that apply below it.

The fold rejects inconsistent lineage and depth beyond the recorded limit. The run index derives parent, root and depth columns from the logs, so the tree can be rebuilt.

**Structured collaboration, not conversation:**
- **Down:** an assignment, rendered as the child's input. Everything the parent model wrote is neutralized, so it cannot forge evidence handles. Earlier results are passed **by reference** (`inputs`: finished delegations of the same parent), verbatim, with their verified evidence recorded in the assignment, and the child's grounding check counts it.
- **Up:** a report built by Hollis from the child's *log*, not from what it says:
  - the outcome Hollis observed (completed, failed, cancelled, timed out, budget exhausted, refused);
  - usage, and tool calls with unknown outcome (propagated from grandchildren);
  - the grounding check of its answer;
  - the evidence it cited, rendered from recorded sources with stable `C-` handles, so the parent's grounding can verify citations;
  - its validated self-report (status, confidence, one-line answer, open questions);
  - its answer, framed as untrusted and neutralized.
  
  The report fits the parent's tool-result limit.
- **Disagreement:** runs asked the same objective that answer differently are reported as disagreements, kept as reported, and never resolved silently.

**Two coordination styles on the same mechanism:**
- **Model-driven:** the agent decides when to delegate, in parallel or in sequence, and when to stop. Hierarchy works within the depth limit.
- **Plans:** a DAG of steps run by the built-in deterministic **workflow coordinator**. Like the demo model, it is code, not a language model, and costs no tokens.
  - Each turn it delegates the next wave of ready steps.
  - It skips steps whose dependencies failed.
  - It compares grouped answers.
  - It answers with a summary or hands off to the final step.
  
  A plan run is an ordinary root run whose input contains the plan, so resumption makes the same decisions.

**Single agent stays the default.** Agents without delegates are unchanged, and nothing in the runtime requires orchestration.

## Alternatives considered

- **A separate orchestration engine** (graph runtime, workflow state machine) with its own persistence. It would duplicate recovery, approvals, budgets and inspection, and drift from them.
- **Shared conversation, group chat or blackboard.** This is the architecture most associated with misalignment failures. It makes permissions, budgets and provenance hard to bound, and shared context is exactly where multi-agent systems help least.
- **Delegation as a new runtime event family** (`DelegationRequested`, ...). It would duplicate the tool lifecycle's authorization, approval and recovery rules. The tool call already *is* the parent's durable intent and outcome.
- **Suspending the parent instead of waiting in the tool.** That needs a new suspension state in the fold. Waiting is bounded by the wait limit and deadline, survives restarts (re-execution attaches), and keeps the runtime unchanged. It is reconsidered if human-in-the-loop children routinely exceed an hour.
- **Random child ids with a lookup table.** That reintroduces a window for duplicate children after a crash.
- **A2A between internal agents.** See [ADR-0032](0032-a2a-interoperability.md): a wire protocol is the wrong layer for in-process durable coordination.

## Trade-offs

- **Gains:** one execution model. Every child is inspectable and resumable. Duplicate children are impossible by construction. Evidence and disagreement survive between agents. Plans give deterministic, token-free coordination when the decomposition is known.
- **Costs:**
  - A waiting delegation holds a coroutine, not a thread, for at most the wait limit (3,500 s at most).
  - Children are created serially per root, so counts and budgets can be checked without races.
  - Model-driven coordination costs tokens and can misroute; the evaluation measures both.
  - Interrupted children on deployments that do not deduplicate requests fail as ambiguous ([ADR-0007](0007-failure-classification-retries-and-ambiguous-policy.md)) rather than retry.

## Consequences

- **Code:**
  - `hollis.runtime.delegation`: lineage, assignment, evidence, limits, child ids, report text helpers.
  - `RunCreated` v6.
  - `Runner.create(run_id=...)`.
  - `hollis.platform.orchestration`: delegate tools, orchestrator, reports, views.
  - `hollis.platform.coordinator`: the workflow coordinator.
  - The run index gains lineage columns (migration 2).
- **Interfaces:** the tree view in REST (`/v1/runs/{id}/tree`), the SDK, the CLI (`hollis runs tree`, `hollis run --plan`) and the web inspector.
- **Tests:**
  - `tests/platform/test_orchestration.py`: parallelism, dependencies, plans, approvals, cancellation, limits, failures, recovery without duplicates;
  - `tests/security/test_orchestration_security.py`;
  - `tests/evals/orchestration`, with findings in [orchestration-evaluation.md](../architecture/orchestration-evaluation.md).
