# Orchestration evaluation

What M7's evaluation measures, how, and what it found, including where multi-agent coordination loses. Regenerate the tables with `uv run python -m tests.evals.orchestration.report`. `tests/evals/orchestration/test_orchestration_eval.py` pins the findings (correctness, isolation, recovery, leakage, bounds), so regressions fail `make check`. Wall-clock times are reported but not asserted.

## What this is, and what it is not

**The agents are simulated.** Every agent "model" is a deterministic policy (`tests/evals/orchestration/sim.py`), not a language model. The policies are:
- `worker`: one search per question and turn, then quote the best-matching evidence sentence with its citation, or say the sources do not say.
- `lead`: route questions to delegates by keyword overlap with their descriptions, delegate all at once, then join the reports.
- `reviewer`: compare reports and prefer the most recent source.

Everything else is the real platform: runner, delegate tools, policy, permission narrowing, limits, budget guard, the workflow coordinator, recovery and views.

**What it shows:**
- what coordination itself changes: context per agent, turns, parallelism, coordination tokens, failure isolation, permission narrowing, budget enforcement, crash recovery and duplicate prevention;
- whether evidence and disagreement survive the trip between agents.

**What it does not show:**
- that real models delegate well, write good assignments, follow the report protocol or resolve conflicts. The simulated reviewer's "prefer the newest source" rule is scripted;
- performance on any real-world task. These results are synthetic, and must not be read as model performance.

**Token counts are estimates.** The simulated agents count tokens with Hollis's estimator over everything a request contains, history included, so long contexts cost what they would. Latency is simulated (20 ms plus 4 µs per input token and 0.2 ms per output token per call) on top of real platform overhead.

**Live evaluation.** `tests/evals/orchestration/test_live_orchestration_eval.py` runs the breadth scenario with a real deployment in every role (`HOLLIS_EVAL_*`, as for the other live evaluations). **It has not been run:** no inference server or API key was available.

## Scenarios and conditions

The corpora are synthetic (`corpus.py`): four domain handbooks (ops, finance, legal, product) with two facts each among similar filler; a service-ownership note for dependent questions; and two refund policies that contradict each other. Each condition runs in a fresh workspace. Agents do not retrieve knowledge at run start (`knowledge_limit=0`), so investigation happens through tools.

| Scenario | Question it answers | Conditions |
|---|---|---|
| independent | 8 questions across 4 collections | single agent at 8 turns (the default budget) and at 16 turns; single agent issuing all searches in one turn; a lead model with 4 specialists; a fixed plan of 4 specialists |
| dependent | the second question needs the first one's answer ("that team") | single agent; a plan chaining the steps with results passed by reference; a naive plan that splits the steps in parallel |
| conflicting | two sources disagree; the newer one is right | single agent with both sources; a plan with one reader per source and a reviewer |
| agent failure | one specialist's deployment rejects every call | single agent whose model fails; lead + specialists; plan |
| interrupted | the process stops while all four specialists are mid-call; reopen and recover | plan, with a retry-safe deployment and with a non-deduplicating one |
| permissions | the caller may read only ops and product | plan as admin; plan as `olive` |
| budget | a tree token budget of half what the lead needed | lead + 4 specialists |
| runaway | an agent that always delegates to itself (one copy, or three) | workspace limits `max_depth=3`, `max_runs=24` |

**Metrics:**
- **correct:** expected fact phrases present in the final answer.
- **verified:** correct facts stated in a claim whose citation Hollis's grounding check verified against evidence the run actually had.
- **tokens:** across every run in the tree.
- **coordination:** tokens of runs that delegated.
- **model calls**, **runs** and **wall** time.

## Results (2026-11-01, offline, Apple M3 Pro)

#### independent

| condition | outcome | correct | verified | tokens | coordination | model calls | runs | wall |
|---|---|---|---|---|---|---|---|---|
| single agent, 8 turns | failed (budget_exhausted) | 0/8 | 0/8 | 10,736 | 0 | 8 | 1 | 0.39s |
| single agent, 16 turns | completed | 8/8 | 8/8 | 13,594 | 0 | 9 | 1 | 0.50s |
| single agent, parallel tool calls | completed | 8/8 | 8/8 | **3,297** | 0 | 2 | 1 | 0.22s |
| lead + 4 specialists | completed | 7/8 | 7/8 | 14,512 | 5,699 | 14 | 5 | 0.39s |
| plan: 4 specialists | completed | 8/8 | 8/8 | 8,486 | 0 | 14 | 5 | 0.22s |

#### dependent

| condition | outcome | correct | verified | tokens | model calls | runs | wall |
|---|---|---|---|---|---|---|---|
| single agent | completed | 2/2 | 2/2 | **1,614** | 3 | 1 | 0.13s |
| plan: chained, results passed | completed | 2/2 | 2/2 | 2,477 | 7 | 3 | 0.25s |
| plan: naive parallel split | completed | 1/2 | 1/2 | 2,158 | 6 | 3 | 0.12s |

#### conflicting

| condition | outcome | correct | tokens | model calls | runs | disagreement |
|---|---|---|---|---|---|---|
| single agent | completed | 1/1 | 678 | 2 | 1 | not surfaced |
| plan: 2 readers + reviewer | completed | 1/1 | 2,504 | 8 | 4 | surfaced, both answers kept |

#### agent failure

| condition | outcome | correct | tokens | coordination | runs | failure |
|---|---|---|---|---|---|---|
| single agent, its model fails | failed (provider_error) | 0/8 | 0 | 0 | 1 | the run fails |
| lead + 4 specialists, finance fails | completed | 5/8 | 12,108 | 5,424 | 5 | reported; nothing invented for finance |
| plan, finance fails | completed | 6/8 | 6,357 | 0 | 5 | reported; nothing invented for finance |

#### interrupted

| condition | outcome | correct | model calls | runs | notes |
|---|---|---|---|---|---|
| plan, stopped mid-delegation (retry-safe deployment) | completed | 8/8 | 18 | 5 | 5 runs resumed; 4 children (4 planned); 4 interrupted calls re-sent |
| plan, stopped mid-delegation (non-deduplicating deployment) | completed | 0/8 | 6 | 5 | 5 runs resumed; 4 children (4 planned); 4 interrupted calls; 4 children failed as ambiguous |

#### permissions

| condition | outcome | correct | tokens | notes |
|---|---|---|---|---|
| plan as admin (all collections) | completed | 8/8 | 8,486 | |
| plan as olive (ops, product only) | completed | 4/8 | 6,738 | 2 runs narrowed; 0 passages from finance or legal in any event of the tree |

#### budget

| condition | outcome | correct | tokens | coordination | runs | notes |
|---|---|---|---|---|---|---|
| lead + 4 specialists, budget 7,256 | completed | 2/8 | 8,569 | 4,966 | 4 | 2 specialists stopped by the budget, 1 delegation refused; the lead reported what it had |

#### runaway

| condition | tokens | model calls | runs | notes |
|---|---|---|---|---|
| self-delegating chain | 3,869 | 7 | 4 | depth 3, the limit; no delegate tool at the deepest level |
| self-delegating fan-out (×3) | 23,510 | 37 | 24 | depth 3; further delegations refused at `max_runs=24` (13–16 in repeated runs, depending on scheduling) |

## What the results say

- **More agents did not mean better results.**
  - On the breadth task, the cheapest correct answer was one agent issuing all its searches in one turn: 3,297 tokens and 2 model calls. Every multi-agent condition used 2.6–4.4× the tokens and 7× the calls.
  - The single agent's real problem was its turn budget. At the default 8 turns, one search per turn could not finish; with 16 turns it could. Multi-agent "fixed" this only because each specialist had its own budget, which buys the same result at a higher price.
- **Context isolation is the real effect.** A plan of four specialists used 38% fewer tokens than the single agent working sequentially (8,486 vs 13,594), because no agent resent everyone else's search results every turn. It also ran in about half the wall time, since the specialists worked concurrently.
- **A model coordinator costs a lot.**
  - The lead spent 5,699 tokens (39% of its tree) on routing and joining.
  - It misrouted one question: its keyword routing sent "production hotfixes" to the product specialist. The specialist said its sources didn't say, so the gap was reported, not invented. But the answer was lost, a coordination failure a single agent cannot have.
  - A fixed plan has no coordinator tokens and no routing errors, when the decomposition is known in advance.
- **Sequential work is worse with multiple agents.**
  - The chained plan was correct, at 53% more tokens and twice the calls of the single agent.
  - The naive parallel split got the dependent question wrong. Passing results by reference (`inputs`) is what keeps dependencies, and splitting dependent work is a decomposition error the platform cannot detect.
- **Disagreement is kept, not hidden.**
  - The single agent happened to pick the right source and never noticed the conflict.
  - The plan detected that two agents answered the same question differently, reported both answers with their runs, and passed both reports, with their verified evidence, to the reviewer.
  - Which answer the reviewer chose is scripted here.
- **Failures are isolated and explicit.**
  - With one specialist's deployment broken, the plan still answered 6/8. It said which part failed and why (`provider_error`), and fabricated nothing.
  - A single agent on that deployment answers nothing. This benefit needs different deployments per agent: if every agent shares one failing deployment, nothing is isolated.
- **Recovery never duplicates, and never pretends.**
  - After stopping the process mid-delegation, every re-executed delegation attached to its existing child: four children, as planned, in both variants.
  - With a deployment that deduplicates requests (Hollis's demo and simulated models), the interrupted calls were re-sent and the plan finished with 8/8.
  - With a real provider's semantics (no deduplication, so the default ambiguous-outcome policy is `fail`), each interrupted call's outcome was unknown. Each child failed with `ambiguous_outcome` instead of risking a second generation, and the plan reported four failed steps.
  - That is the intended trade-off ([ADR-0007](../decisions/0007-failure-classification-retries-and-ambiguous-policy.md)), not a bug. Interruption costs answers, not correctness.
- **Permissions only narrow.** The caller without finance and legal grants got 4/8. Two runs were recorded as narrowed. No passage from the hidden collections appeared anywhere in the tree's event logs.
- **Budgets bound the tree, and leave a reserve.**
  - With half the tokens it needed, the lead finished and reported 2/8 with explicit gaps. Delegated work stopped at 75% of the budget; one delegation was refused, two specialists were stopped.
  - The tree used 8,569 tokens against a 7,256 budget. The overshoot is the lead's own final call: admitted with budget left, it then read four reports. That is the documented bound (a run's own call may use the rest of the budget, plus its estimated input's error), so size budgets with the final report in mind.
- **Runaway delegation stops.** A self-delegating agent stopped at depth 3. A self-delegating fan-out stopped at exactly 24 runs, with every further delegation refused and recorded. How many were attempted depends on scheduling; the cap does not.

## Limitations

- **Simulated agents.** Simulated agents follow instructions perfectly and never write a vague assignment. Real coordinators do, which is the dominant failure in the literature. Expect real results to look worse for model-driven coordination than here.
- **Tasks and corpora.** The tasks are small and the corpora synthetic. The questions are not a benchmark.
- **Token counts.** Tokens are Hollis estimates. Real tokenizers differ, and real models produce longer outputs.
- **Latency.** Wall-clock numbers use simulated model latency and are machine-dependent.
- **Live runs.** No live run has been made.
