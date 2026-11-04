# Specialist evaluation

What M8's evaluation measures, how, and what it found, including where specialization costs more or does not help. Regenerate the tables with `uv run python -m tests.evals.specialists.report` (about 3 minutes of CPU). `tests/evals/specialists/test_specialist_eval.py` runs a smaller grid of the same code and pins the findings, so regressions fail `make check`. Wall-clock times are reported but not asserted.

## What this is, and what it is not

**The agent is simulated.** Every run's "model" is a deterministic policy (`tests/evals/specialists/sim.py`), not a language model. It uses whatever tools the run offers: exact SQL when it has `data_query`, otherwise reading the file; code search with line numbers, otherwise listing and reading files; patches and the configured test command when offered. It submits under the run's contract when a `submit_*` tool is offered. Otherwise it answers with the same JSON, so every condition is graded the same way. Everything else is the real platform: runner, tools, workspace policy, approvals, overlays, templates, contracts and their checks.

**What it shows:**
- what the parts of a specialization change mechanically, with no assumption about model behaviour: which tools make a task possible, what a contract's checks catch, what overlays and smaller tool sets take off the table, and what it all costs in tokens, calls and time;
- given an explicit error model applied identically in every condition: how often each condition delivers a correct result, a wrong one silently, or a visible failure.

**What it does not show:**
- **whether a procedure (the template's instructions) changes a real model's behaviour.** The simulated agent never reads its instructions. That is the question the live evaluation is for;
- whether real models make errors at these rates, or act on rejections at these rates;
- performance on real-world tasks. The tasks are small and synthetic. These results must not be read as model performance.

**Live evaluation.** `tests/evals/specialists/test_live_specialist_eval.py` runs the same tasks and grading with a real deployment in every role (`HOLLIS_EVAL_*`, as for the other live evaluations). **It has not been run:** no inference server or API key was available.

## Tasks

Each task has a known answer (`corpus.py`).

| Task | What is asked | Template | Graded by |
|---|---|---|---|
| data, 40 rows | Q3 revenue per region and the leading region, from a CSV that fits in one tool result | `data-analyst` | every total (to the cent) and the leader |
| data, 3,000 rows | the same, from a CSV larger than one tool result (16,000 characters) | `data-analyst` | the same |
| code review | three seeded defects (off-by-one slice, SQL f-string, `==` on a token) among clean files | `code-reviewer` | exactly the three defect lines, nothing else |
| research | three facts from four policy documents, two of which disagree (the newer one is in force) | `researcher` | each statement has the fact and cites a passage containing it |
| bug fix | an off-by-one in `mean()`, with a unit test configured as the `run_tests` command | `software-engineer` | the code passes the test; claims of passing are true |

## Conditions

All three use the same simulated agent, model budget and error draws.

- **generalist**: generic file and knowledge tools (`fs_list_dir`, `fs_read_file`, `fs_write_file`, `knowledge_search`, `knowledge_read`) and generic instructions; no contract, no overlay.
- **generalist + tools**: the same, plus the template's tools (code, data, docs, patch, configured commands).
- **specialist**: an agent made from the template (its tools, procedure, overlay and contract).

Approvals are decided as a person would: the change the bug-fix task asked for is approved, and writes nobody asked for (review) are denied and counted.

## Error model

Each error kind occurs independently with probability **p** per item:
- data: a wrong number (+7%), or the wrong leading region;
- review: a finding two lines off (inside what was read), or a made-up finding in a file that doesn't exist, or an unrequested write;
- research: a fabricated citation handle, or a made-up statement with a real citation;
- bug fix: a wrong first patch, or a report claiming the tests pass.

The draws are hashed from (seed, task, kind, item), never from the condition, so every condition meets the same errors at the same places.

When a submission is rejected, each error the rejection names is fixed with probability **r** on each later attempt (at most three submissions). Wrong patches are fixed with the same probability when the tests fail. Some kinds are **detectable** by a contract (a number absent from the query result, a non-existent file, a fabricated handle, a passing claim without a passing run). Others are not (a wrong judgement on real data, a near-miss line the run did see, a made-up statement with a real citation).

Outcomes per run:
- **correct**: the delivered result is right;
- **silent error**: a result was delivered as usable, and it is wrong;
- **visible failure**: no usable result (an abstention, a rejected output, a reported failure, or an unverified change).

## Results

30 seeded instances per row and cell. Tokens are the simulated agent's estimates over everything each request contains (instructions, tool definitions, history), so longer instructions and schemas cost what they would. Times are platform overhead only: the simulated model answers instantly.

### Cost and scope, with no injected errors (p = 0)

| Task | Condition | Correct | Tokens | Model calls | Tool calls | Tools offered | Of which can change things | Median wall time |
|---|---|---|---|---|---|---|---|---|
| data, 40 rows | generalist | 100% | 2387 | 2.0 | 1.0 | 5 | 1 | 19 ms |
| data, 40 rows | generalist + tools | 100% | 3222 | 2.0 | 1.0 | 8 | 1 | 19 ms |
| data, 40 rows | specialist | 100% | 5557 | 3.0 | 2.0 | 3 | 0 | 26 ms |
| data, 3,000 rows | generalist | 0% | 2621 | 2.0 | 1.0 | 5 | 1 | 20 ms |
| data, 3,000 rows | generalist + tools | 100% | 3230 | 2.0 | 1.0 | 8 | 1 | 21 ms |
| data, 3,000 rows | specialist | 100% | 5574 | 3.0 | 2.0 | 3 | 0 | 29 ms |
| code review | generalist | 100% | 3714 | 3.0 | 5.0 | 5 | 1 | 38 ms |
| code review | generalist + tools | 100% | 5937 | 3.0 | 6.0 | 8 | 1 | 43 ms |
| code review | specialist | 100% | 9732 | 4.0 | 7.0 | 3 | 0 | 53 ms |
| research | generalist | 100% | 3710 | 2.0 | 3.0 | 5 | 1 | 59 ms |
| research | generalist + tools | 100% | 4203 | 2.0 | 3.0 | 7 | 1 | 59 ms |
| research | specialist | 100% | 9920 | 3.0 | 4.0 | 4 | 0 | 73 ms |
| bug fix | generalist | 100% | 3171 | 3.0 | 2.0 | 5 | 1 | 33 ms |
| bug fix | generalist + tools | 100% | 8375 | 4.0 | 3.0 | 10 | 2 | 70 ms |
| bug fix | specialist | 100% | 13460 | 5.0 | 4.0 | 5 | 1 | 81 ms |

### Injected errors: p = 0.3 per error kind, fix rate after a rejection r = 0.5

| Task | Condition | Correct | Silent errors | Visible failures | Valid references | Detectable errors caught | Approvals asked |
|---|---|---|---|---|---|---|---|
| data, 40 rows | generalist | 23% | 77% | 0% | 71% | 0% | 0.00 |
| data, 40 rows | generalist + tools | 23% | 77% | 0% | 71% | 0% | 0.00 |
| data, 40 rows | specialist | 43% | 30% | 27% | 92% | 100% | 0.00 |
| data, 3,000 rows | generalist | 0% | 0% | 100% | 0% | - | 0.00 |
| data, 3,000 rows | generalist + tools | 23% | 77% | 0% | 71% | 0% | 0.00 |
| data, 3,000 rows | specialist | 43% | 30% | 27% | 92% | 100% | 0.00 |
| code review | generalist | 20% | 80% | 0% | 66% | 0% | 0.47 |
| code review | generalist + tools | 20% | 80% | 0% | 66% | 0% | 0.47 |
| code review | specialist | 27% | 70% | 3% | 70% | 100% | 0.00 |
| research | generalist | 3% | 97% | 0% | 38% | 0% | 0.00 |
| research | generalist + tools | 3% | 97% | 0% | 38% | 0% | 0.00 |
| research | specialist | 13% | 60% | 27% | 59% | 100% | 0.00 |
| bug fix | generalist | 70% | 3% | 27% | 63% | 0% | 1.00 |
| bug fix | generalist + tools | 90% | 0% | 10% | 100% | - | 1.20 |
| bug fix | specialist | 90% | 0% | 10% | 100% | - | 1.20 |

"Detectable errors caught" counts runs whose first result had a detectable error, and were rejected for it (`-`: no such runs). "Valid references" are delivered claims whose value, line, citation or verification is right.

### Sensitivity: correct / silent errors across error models

| Task | Condition | p 0.1, r 0.0 | p 0.1, r 0.5 | p 0.1, r 1.0 | p 0.3, r 0.0 | p 0.3, r 0.5 | p 0.3, r 1.0 |
|---|---|---|---|---|---|---|---|
| data, 40 rows | generalist | 60% / 40% | 60% / 40% | 60% / 40% | 23% / 77% | 23% / 77% | 23% / 77% |
| data, 40 rows | generalist + tools | 60% / 40% | 60% / 40% | 60% / 40% | 23% / 77% | 23% / 77% | 23% / 77% |
| data, 40 rows | specialist | 60% / 7% | 80% / 13% | 87% / 13% | 23% / 3% | 43% / 30% | 67% / 33% |
| data, 3,000 rows | generalist | 0% / 0% | 0% / 0% | 0% / 0% | 0% / 0% | 0% / 0% | 0% / 0% |
| data, 3,000 rows | generalist + tools | 60% / 40% | 60% / 40% | 60% / 40% | 23% / 77% | 23% / 77% | 23% / 77% |
| data, 3,000 rows | specialist | 60% / 7% | 80% / 13% | 87% / 13% | 23% / 3% | 43% / 30% | 67% / 33% |
| code review | generalist | 70% / 30% | 70% / 30% | 70% / 30% | 20% / 80% | 20% / 80% | 20% / 80% |
| code review | generalist + tools | 70% / 30% | 70% / 30% | 70% / 30% | 20% / 80% | 20% / 80% | 20% / 80% |
| code review | specialist | 70% / 23% | 73% / 27% | 73% / 27% | 20% / 57% | 27% / 70% | 30% / 70% |
| research | generalist | 50% / 50% | 50% / 50% | 50% / 50% | 3% / 97% | 3% / 97% | 3% / 97% |
| research | generalist + tools | 50% / 50% | 50% / 50% | 50% / 50% | 3% / 97% | 3% / 97% | 3% / 97% |
| research | specialist | 50% / 17% | 60% / 23% | 67% / 33% | 3% / 30% | 13% / 60% | 17% / 83% |
| bug fix | generalist | 90% / 0% | 90% / 0% | 90% / 0% | 70% / 3% | 70% / 3% | 70% / 3% |
| bug fix | generalist + tools | 90% / 0% | 100% / 0% | 100% / 0% | 70% / 3% | 90% / 0% | 100% / 0% |
| bug fix | specialist | 90% / 0% | 100% / 0% | 100% / 0% | 70% / 0% | 90% / 0% | 100% / 0% |

## Findings

1. **Specialization costs tokens on every run.** With no errors, the specialist used 2.3× (data) to 4.2× (bug fix) the generalist's tokens, and one more model call (the submission). The cost comes from longer instructions, the contract's schema in the tool definitions, and both being re-sent each turn. Adding only the tools ("generalist + tools") costs part of it. When nothing goes wrong, all three get the same answers wherever the generic tools suffice.
2. **Tools decide what is possible at all.** The 3,000-row table does not fit in one tool result, so the generalist cannot compute exact totals. It abstains (a visible failure, never a silent one). With `data_query`, both other conditions answer exactly. Without a test command, the generalist's bug fix stays unverified (27% visible failures at p = 0.3).
3. **Contracts turn detectable errors into visible failures.** With no fixing at all (r = 0, p = 0.3), every detectable error was caught. The specialist's silent-error rate fell to 3% against 77% (data), 30% against 97% (research) and 57% against 80% (review). The generalist conditions delivered the same errors silently.
4. **With feedback, caught errors become correct results.** Data at p = 0.3: 23% correct without a contract, then 43% at r = 0.5 and 67% at r = 1 with one. Without a contract, the fix rate changes nothing, because nothing tells the agent what is wrong.
5. **Errors a contract cannot see stay silent, and acceptance is not correctness.** Contracts check references: values in query results, lines the run saw, evidence it received, commands it ran. They do not check judgement:
   - the wrong leading region (computed from real numbers);
   - a finding two lines off, on a line the reviewer did read;
   - a made-up statement with a real citation.

   These stay silent in every condition. At r = 1 the specialist's silent rate is even *higher* than at r = 0 (data 33% vs 3%, research 83% vs 30%). Fixing the caught error lets the output be accepted, together with an uncaught error in the same output. An accepted output means "its references were verified", and the interfaces say so.
6. **Smaller tool sets and overlays take actions off the table.** The data, review and research specialists are offered no tool that can change anything (the generalists have `fs_write_file`). In review, unrequested writes reached a human approver in 47% of generalist runs and never for the specialist. The engineering specialist is offered one changing tool (`apply_patch`) against two for generalist + tools.
7. **Where specialization adds nothing here.**
   - On the bug fix, generalist + tools and the specialist reach the same correctness at every grid point: the test command, not the contract, catches wrong patches. The contract only removed the 3% of runs that falsely claimed passing tests (p = 0.3, r = 0).
   - On data, review and research, adding the specialist's tools to a generalist changed nothing: the generic tools sufficed, except for the large table.

## Reproducing

```bash
uv run python -m tests.evals.specialists.report        # these tables (30 seeds, 3,150 runs)
uv run pytest tests/evals/specialists -q               # the pinned findings (6 seeds)
HOLLIS_EVAL_BASE_URL=... HOLLIS_EVAL_MODEL=... uv run pytest tests/evals/specialists -m live -s
```
