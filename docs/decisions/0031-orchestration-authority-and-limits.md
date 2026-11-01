# ADR-0031: Authority, limits and budgets for delegated runs

- Status: Accepted (M7)
- Date: 2026-11-01

## Context

Delegation multiplies everything an agent can do: more model calls, more tool calls, more access paths. Delegated runs must never gain more permissions than their caller, their parent agent and the configured policies allow (NFR-4). Recursion, unbounded spawning and runaway consumption must be impossible (NFR-6). Other agents' output is untrusted input. Authority is checked live, not cached in identifiers ([ADR-0028](0028-authentication-and-authorization.md)).

## Decision

**Permissions only narrow.**
- **Memory and knowledge:** a child's access is its agent's configuration, within the parent run's effective access, within the run owner's *current* grants. A scope is writable only if also readable. What was removed is recorded on the child (`narrowed`) and shown.
- **Owner:** a delegated run belongs to the root's owner, so the same visibility rules apply. Each delegation re-checks that the owner still exists and may run agents.
- **Tool calls:** a child's tool calls are decided by the same workspace policy. The delegation depth is an additional policy fact, so rules can treat delegated runs differently (`delegated = true`).
- **Approvals:** approving a child's call needs the approve permission and access to the child run, through the same binding and expiry checks.
- **Delegate tool effect:** a delegate tool's declared effect is the strongest effect its delegation can reach, transitively. Unknown and MCP tools count as mutating. So the default policy lets read-only delegations run, and asks for approval before delegating to an agent that can change things. The child's own side effects are still decided individually.
- **Delegate tools themselves:** a model can call only the delegate tools its run was given, which are the agent's configured delegates. Results can be passed only between finished delegations of the same parent; an identifier alone grants nothing.

**Limits are recorded and inherited, never widened.** `DelegationLimits` (workspace `[orchestration]`, narrowed per agent) records:
- `max_depth` (absolute ceiling 5): runs at the limit get no delegate tools, and the fold rejects deeper lineage;
- `max_children` per run and `max_runs` per tree, checked under a per-root lock;
- `token_budget` for the tree;
- `wait_s`, also capped by the parent's deadline.

A child's limits are its parent's, narrowed field by field by its agent's overrides: only the fields an agent sets narrow; unset fields inherit.

**Token budgets apply to subtrees, with a reserve:**
- **The guard:** a `ModelCallGuard` (a runtime port the platform implements) is asked before every model call in a tree. It refuses (fail closed) once a budget on the run's path is used up; the run fails with `budget_exhausted`, explained.
- **The reserve:** work below a run may use 75% of the run's budget, for both new delegations and the delegated runs' model calls. The rest is the run's reserve to report what it has, and its own calls may use the full budget.
- **Reservations:** an admitted call reserves its output budget plus its estimated input until its outcome is recorded, so concurrent calls count each other. Only input-estimate error, and a run's own final call, can overshoot.
- **Per run:** `RunBudget.max_total_tokens` caps one run, enforced by the runner and the fold.

**No orphans, no silent loss:**
- Cancelling a run cancels its descendants top-down, each run before its children, so no run reacts by delegating again. A delegation that stops waiting cancels the child's whole branch, and recovery cancels runs whose parent already finished.
- A child that cannot finish within the wait limit, or whose execution stopped unexpectedly, is cancelled, and its parent gets a report saying why.
- Tool calls with unknown outcome anywhere below a run are listed in its report.
- Nothing claims exactly-once execution.

**Other agents' output is data:**
- Child answers are framed and neutralized, so they cannot forge evidence blocks or close their frame.
- Evidence crosses runs only as Hollis-rendered blocks from recorded sources.
- Self-reports are validated to fixed vocabularies.
- Delegated context is checked for registered secrets, like any run input.

## Alternatives considered

- **Children inherit the parent's access wholesale.** A specialist with narrower needs would see everything; least privilege per agent is the point of specialists.
- **Children may exceed the parent's access when configured** (a "privileged specialist"). The parent model could then launder access: ask the child to fetch, then read its report. Rejected: what a child can read, its parent can effectively read.
- **Requiring child tools ⊆ parent tools.** Unnecessary for safety (each child call is decided by policy and approval), and it would force orchestrators to hold every tool themselves.
- **Static budget splitting** (each child gets budget/N). Wasteful for uneven work and wrong under dynamic delegation. The subtree check with a reserve adapts.
- **A reservation ledger in the database.** Budgets are enforced in the one process that owns the workspace ([ADR-0004](0004-local-first-sqlite-persistence.md)). In-memory reservations are lost on a crash, which only loosens the bound until calls record usage.

## Trade-offs

- **Gains:** delegation cannot widen access, outlive its parent, recurse without bound, or exceed a budget beyond a stated bound. Every refusal is explained in the tree.
- **Costs:**
  - Orchestrator agents must be configured with the access their specialists need, or children get none. The narrowing is shown, not hidden.
  - Approval-required delegations add a human step.
- An agent edit that would change a delegate tool's identity (its description, or the effect it can reach) is refused while unfinished runs use that tool, because those runs could not resume.
  - Budgets are checked against provider-reported usage after each call and estimates before it, so they are bounds, not exact meters.

## Consequences

- **Runtime:** `PolicyRequest.depth`, `Rule(delegated=...)`, `RunBudget.max_total_tokens` and the `ModelCallGuard` port.
- **Platform:** grant narrowing, limits, the guard and cascade in `hollis.platform.orchestration` and the services.
- **Tests:**
  - narrowing, owner checks, input scoping, forged evidence, frame escapes and secrets are tested, and the key checks are mutation-checked;
  - budget, reserve and recursion bounds are tested and measured in the [evaluation](../architecture/orchestration-evaluation.md).
