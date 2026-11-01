# Multi-agent orchestration

Hollis can let one agent hand bounded tasks to other agents and use their results. Each delegated task is an ordinary, durable run of the other agent:
- it has its own model, tools, memory and knowledge;
- you can inspect, approve, cancel and resume it like any run;
- it is linked to the run that delegated it.

**Use a single agent unless you have a reason not to.** In Hollis's [evaluation](../architecture/orchestration-evaluation.md):
- a single agent issuing parallel tool calls was the cheapest correct answer on a breadth task;
- multiple agents cost more and helped nothing on sequential work.

Delegation pays off when:
- **the work splits into independent parts:** they run concurrently, each in a small context;
- **the parts need different models, tools or access:** least privilege per agent;
- **a part may fail and the rest should still finish.**

## Delegating agents

Give an agent `delegates`. It then gets one `delegate_<agent>` tool per delegate:

```bash
hollis agents create researcher -m local-qwen --tool knowledge_search --collection docs \
  --description "answers from the engineering handbook"
hollis agents create lead -m local-qwen --delegate researcher --delegate reviewer \
  --collection docs --token-budget 200000
hollis run -a lead "Prepare a release brief"
```

```python
with Hollis.open() as hollis:
    hollis.agents.create(name="lead", model="local-qwen", delegates=("researcher", "reviewer"))
    run = hollis.runs.start("Prepare a release brief", agent="lead")
    run.wait()
    print(run.tree().totals)
```

The model decides when to delegate. Each call gives the delegate:
- an **objective**: one bounded task;
- optional **context**: the delegate sees nothing else of the conversation;
- an optional **expected output**;
- optional **inputs**: earlier delegations whose reports it should receive verbatim, so nothing is retyped.

Independent read-only delegations in one turn run at the same time. Dependent work goes in a later turn, passing the earlier results as `inputs`.

The delegate's description is shown to the model in the tool's description. Write it as "what this agent is for".

**With the demo model**, `@agent` in a task delegates to that agent, and `then` starts a dependent step: `hollis run -a lead "@researcher find X and @analyst check Y, then @reviewer compare them"`.

## Plans

When you know the decomposition, write it down instead of asking a model to coordinate. A plan costs no coordinator tokens and cannot misroute:

```toml
# release.toml: hollis run --plan release.toml "Prepare the release brief"
answer = "summary"          # or "last": hand off to the one final step

[[steps]]
id = "deploy"
agent = "researcher"
objective = "How do we deploy a release?"

[[steps]]
id = "budget"
agent = "analyst"
objective = "What is the travel budget?"

[[steps]]
id = "review"
agent = "reviewer"
objective = "Check both answers against their evidence."
depends_on = ["deploy", "budget"]
```

```python
from hollis import Plan, PlanStep

hollis.runs.start(
    "Release brief", plan=Plan(steps=(PlanStep(id="a", agent="researcher", objective="..."),))
)
```

A plan runs on the built-in **workflow coordinator**: deterministic code, not a model.
- Each turn it delegates every step whose dependencies completed, passing their reports along.
- A step whose dependency failed is skipped, and the answer says so.
- Steps given the same objective have their answers compared. Disagreements are reported as they are, never resolved silently.

## What a delegating agent receives

Each delegation returns a **report** built by Hollis from the delegate's run log, not from what the delegate claims:
- the outcome (completed, failed, cancelled, timed out, budget exhausted, or refused before it started);
- usage, and tool calls whose outcome is unknown, from anywhere below it;
- a check of its citations;
- the evidence it cited, rendered from its recorded sources with stable `[C-…]` handles the parent can cite and verify;
- its self-reported status and confidence;
- its answer, framed as model output, not fact.

## Permissions only narrow

- **Delegate's access:** its memory and knowledge are its own configuration, within what the delegating run had, within its owner's current grants. Configure an orchestrator with the collections its specialists need; what a child was not given is shown as "not granted".
- **Ownership:** a delegated run belongs to whoever started the root run. Its tool calls go through the same policy.
- **Approvals:** come from someone allowed to approve on that run. Rules can treat delegated runs differently (`delegated = true` in a policy rule).
- **Delegating to an agent that can change things** needs approval under the default policy. Delegating to a read-only agent does not.

## Limits

Set workspace-wide in `[orchestration]` ([configuration](../reference/configuration.md)). An agent's `delegation` can only narrow them.

| Limit | Default | |
|---|---|---|
| `max_depth` | 2 | delegation levels below the root (absolute ceiling 5); runs at the limit get no delegate tools |
| `max_children` | 8 | delegations per run |
| `max_runs` | 24 | runs per orchestration, the root included |
| `token_budget` | none | tokens for the whole orchestration |
| `wait_s` | 900 | how long a run waits for one delegate (also capped by its deadline) |

**How a token budget is enforced:**
- Work below a run may use 75% of the run's budget. The rest is kept so the delegating run can report what it has.
- Model calls in flight reserve their output budget and estimated input.
- The run's own final call can overshoot by its own size, so size budgets for the final report.

A delegate that runs out of time is cancelled, and the parent is told. Cancelling a run cancels everything it delegated.

## Inspecting

- **Web:** a run that delegated, or was delegated, has an **Orchestration** section, including the timeline, refusals and disagreements. It shows:
  - every run in the tree: agent, objective, outcome, usage, citation check;
  - what each run is waiting on and what it was not granted;
  - unknown side effects and data dependencies.
- **CLI:** `hollis runs tree RUN` and `hollis runs list --roots`.
- **API:** `GET /v1/runs/{id}/tree`.

## Recovery

If the process stops mid-delegation, recovery continues every run, and each interrupted delegation **attaches to its existing delegate run**; it never starts a second one. A delegate whose model call was interrupted follows its deployment's ambiguous-outcome policy:
- **deployments that deduplicate requests:** the call is retried;
- **real providers:** they don't deduplicate, so by default the delegate fails as ambiguous rather than risk a second generation, and the parent is told.

Hollis never claims exactly-once execution.

## Not yet

- Agents from other vendors over A2A ([ADR-0032](../decisions/0032-a2a-interoperability.md)).
- Any test with a live model as coordinator or specialist.
