# Memory and context evaluation

What M4's evaluation measures, how, and the results it actually produced. Regenerate the tables with `uv run python -m tests.evals.report`. `tests/evals/test_memory_eval.py` asserts thresholds below these results, so regressions fail `make check`.

## What is measured

**Context sufficiency:** does the context Hollis would give a model contain the evidence a probe needs, and none of the evidence it must not rely on, within a fixed input budget (1,200 tokens)?

This is a model-free proxy. It shows whether memory can supply the right information; it does **not** show that a given model then answers correctly. The opt-in live evaluation (`tests/evals/test_live_memory_eval.py`, `HOLLIS_EVAL_*`) asks a real deployment and grades its answers by keyword. **It has not been run:** no inference server or API key was available. Ollama is installed on this machine, but it has no models.

**Scenarios.** Deterministic, seeded multi-session agent trajectories (`tests/evals/scenario.py`). Planted statements carry the evidence. Seeded noise statements (other services, owners, regions, versions, machine-generated log lines) share vocabulary with them and push the history far past the budget.
- **`ops-assistant`:** the scenario retrieval was tuned on. Checked across 10 noise seeds and two noise levels: memory was 100% clean on all of them.
- **`research-assistant`:** a held-out scenario, written *after* tuning and not tuned against. It uses a different domain, and its questions are paraphrased away from the stored wording.

**Probe categories:**
- From LongMemEval: information extraction, multi-session reasoning, knowledge update, temporal reasoning, abstention.
- From MemoryAgentBench: accurate retrieval, test-time learning (via updates), long-range understanding, selective forgetting.
- Temporary conditions.
- Causal evidence from agent logs, the AMA-Bench emphasis.
- Cross-user isolation.

**Conditions at the same budget:**
- `no_memory`: nothing beyond the question.
- `context_only`: the current session only.
- `raw_history`: the whole history, keeping the newest that fits.
- `memory`: statements written through the governed write path (user statements as confirmed facts with their slot semantics, tool output as observed, deletions via `forget`), then recalled as of the probe time.

**Metrics:**
- *evidence*: required statements present.
- *clean*: required statements present, and no stale, corrected, expired, deleted or other-user statement present.
- Stale or wrong evidence offered; leakage; deleted evidence offered.
- Mean context tokens (estimated); recall latency; records and store size.

**Compaction fidelity.** A scripted 32-turn run with planted facts in early tool results and periodic side effects (`tests/evals/compaction.py`). It's compacted repeatedly, under the deterministic summarizer and under a "forgetful" model-like summarizer that never repeats earlier notes. That is compared with a simulation of naive summary-of-summary replacement.

## Results (2026-10-31, offline, SQLite store, Apple M3 Pro MacBook Pro)

#### ops-assistant-10x12 (budget 1200 tokens, 12 probes)

| condition | evidence | clean | stale/wrong offered | leakage | deleted offered | mean tokens |
|---|---|---|---|---|---|---|
| no_memory | 0% | 25% | 0% | 0% | 0% | 0 |
| context_only | 0% | 25% | 0% | 0% | 0% | 233 |
| raw_history | 33% | 42% | 0% | 0% | 100% | 1128 |
| memory | 100% | 100% | 0% | 0% | 0% | 352 |

| category | no_memory | context_only | raw_history | memory |
|---|---|---|---|---|
| abstention | 100% | 100% | 100% | 100% |
| agent_logs | 0% | 0% | 0% | 100% |
| forgetting | 100% | 100% | 0% | 100% |
| information_extraction | 0% | 0% | 0% | 100% |
| isolation | 100% | 100% | 100% | 100% |
| knowledge_update | 0% | 0% | 50% | 100% |
| multi_session | 0% | 0% | 0% | 100% |
| preference | 0% | 0% | 0% | 100% |
| temporal | 0% | 0% | 100% | 100% |
| temporary | 0% | 0% | 50% | 100% |

Memory: 120 records after ingest, recall p50 1.9 ms / p95 2.2 ms, ingest 135 ms, store 292 KiB.

#### ops-assistant-40x12 (budget 1200 tokens, 12 probes)

| condition | evidence | clean | stale/wrong offered | leakage | deleted offered | mean tokens |
|---|---|---|---|---|---|---|
| no_memory | 0% | 25% | 0% | 0% | 0% | 0 |
| context_only | 0% | 25% | 0% | 0% | 0% | 976 |
| raw_history | 0% | 25% | 0% | 0% | 0% | 1148 |
| memory | 100% | 100% | 0% | 0% | 0% | 458 |

| category | no_memory | context_only | raw_history | memory |
|---|---|---|---|---|
| abstention | 100% | 100% | 100% | 100% |
| agent_logs | 0% | 0% | 0% | 100% |
| forgetting | 100% | 100% | 100% | 100% |
| information_extraction | 0% | 0% | 0% | 100% |
| isolation | 100% | 100% | 100% | 100% |
| knowledge_update | 0% | 0% | 0% | 100% |
| multi_session | 0% | 0% | 0% | 100% |
| preference | 0% | 0% | 0% | 100% |
| temporal | 0% | 0% | 0% | 100% |
| temporary | 0% | 0% | 0% | 100% |

Memory: 356 records after ingest, recall p50 2.5 ms / p95 3.0 ms, ingest 523 ms, store 784 KiB.

#### research-assistant-40x10 (budget 1200 tokens, 11 probes)

| condition | evidence | clean | stale/wrong offered | leakage | deleted offered | mean tokens |
|---|---|---|---|---|---|---|
| no_memory | 0% | 27% | 0% | 0% | 0% | 0 |
| context_only | 0% | 27% | 0% | 0% | 0% | 1022 |
| raw_history | 0% | 27% | 0% | 0% | 0% | 1149 |
| memory | 88% | 91% | 0% | 0% | 0% | 508 |

| category | no_memory | context_only | raw_history | memory |
|---|---|---|---|---|
| abstention | 100% | 100% | 100% | 100% |
| agent_logs | 0% | 0% | 0% | 0% |
| forgetting | 100% | 100% | 100% | 100% |
| information_extraction | 0% | 0% | 0% | 100% |
| isolation | 100% | 100% | 100% | 100% |
| knowledge_update | 0% | 0% | 0% | 100% |
| multi_session | 0% | 0% | 0% | 100% |
| preference | 0% | 0% | 0% | 100% |
| temporal | 0% | 0% | 0% | 100% |
| temporary | 0% | 0% | 0% | 100% |

Memory: 332 records after ingest, recall p50 2.3 ms / p95 2.5 ms, ingest 453 ms, store 740 KiB.

#### Compaction fidelity

| summarizer | turns | compactions | side effects kept | notes kept | input in every request | within budget | final tokens | uncompacted history tokens |
|---|---|---|---|---|---|---|---|---|
| deterministic | 32 | 6 | 100% | 100% | yes | yes | 1187 | 13557 |
| forgetful | 32 | 6 | 100% | 100% | yes | yes | 1692 | 13557 |
| naive_replace (simulated) | | | n/a | 17% | | | | |


## What the results show

- **Memory beats the baselines by a wide margin at equal budget.**
  - Clean evidence: 100% (tuned scenario) and 91% (held-out) for memory, against 25–42% for raw history and 25–27% for no memory or context-only.
  - Memory used about 40% of raw history's tokens.
  - As history grows, raw history falls to the trivial floor: the only probes it "passes" are the ones where nothing is required.
- **Raw history offers wrong evidence that memory filters out.** In the small scenario it offered the deleted phone number and, in the held-out one, stale facts. Memory offered no stale, corrected, expired, deleted or other-user evidence in any run.
- **The first run showed multi-session evidence at 0%.** That diagnosis led to two recall changes (ADR-0021): the subject bonus now requires the whole subject in the query, and recall follows entity links from the best matches. The held-out scenario then scored 100% on its multi-session probe without tuning.
- **Known gap.** A causal link through a machine identifier (`CUDA_ERROR_OUT_OF_MEMORY` in a log → the procedure stored as "CUDA out of memory") isn't retrieved. It's pinned as a strict `xfail`.
- **Compaction with carry-forward keeps 100% of side effects and notes over 6 compactions.** Simulated naive replacement keeps 17% of notes. The objective is in every request, every estimate is within budget, and the final request is roughly 8–11× smaller than the uncompacted history.
- **Cost:** recall is a few milliseconds (p95 under 4 ms at a few hundred records). Ingestion is dominated by one SQLite transaction per write.

## Limitations of this evaluation

- **Synthetic, and written by the implementer.** The held-out scenario reduces, but doesn't remove, the risk of overfitting. No public benchmark (LongMemEval, MemoryAgentBench, AMA-Bench) was run: they need model access, and their datasets weren't downloaded. Running them is recommended once a deployment is available.
- **Trivially passed probes.** Abstention, isolation and forgetting probes are "clean" whenever nothing forbidden appears, so the empty conditions pass them trivially. Whether a model abstains needs the live evaluation.
- **Ideal extraction.** Statements enter memory through the write path with their slots supplied. This measures storage and retrieval, not the quality of model-assisted extraction or of model-written memories.
- **Estimated tokens:** token counts are the heuristic estimate, not a provider tokenizer.
- **One machine:** latency was measured on one laptop (Apple M3 Pro). Scale beyond a few thousand records per scope wasn't measured.
