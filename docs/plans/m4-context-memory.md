# M4 plan: context engineering and memory

Branch `m4-context-memory`, from `m3-tools`. Owned end to end. No separate approval step.

## Starting point (M3)

- Every model call resends the run's whole history: input, every assistant turn, every tool result. Long runs grow without bound, until the provider rejects the request or `max_model_turns` stops the run.
- Tool results above `max_tool_output_chars` are truncated, and the rest is lost.
- There's no token accounting before a call, only provider-reported usage after it. Deployments don't declare a context window.
- Nothing persists across runs except the run logs themselves.

## Live verification status

- Ollama 0.24.0 is installed, but no models are pulled. Nothing else serves inference locally. No API keys are set.
- So the M2/M3 live tests and the live memory evaluation can't run here. Downloading models (gigabytes) wasn't requested, so I didn't.
- All M4 checks are offline. Live evaluations exist as opt-in tests.

## Research (2026-10-31)

- **MemoryAgentBench** (Hu, Wang, McAuley; ICLR 2026; arXiv 2507.05257):
  - Four competencies: accurate retrieval, test-time learning, long-range understanding, selective forgetting (conflict resolution).
  - Trajectories are fed incrementally as multi-turn chunks.
  - Plain BM25 far outperformed a long-context baseline on multi-query retrieval (100% vs 22.8%). No method masters all four.
- **AMA-Bench** (arXiv 2602.22769; ICML 2026):
  - Agent memory is mostly machine-generated interaction logs, not chat.
  - Memory systems over-rely on similarity retrieval and miss causal and temporal structure. Long-context baselines beat most memory systems.
  - Their best system uses a causality graph plus *tool-augmented retrieval*.
- **LongMemEval** (ICLR 2025): five abilities (information extraction, multi-session reasoning, temporal reasoning, knowledge updates, abstention). Proposes session decomposition, fact-augmented keys and time-aware query expansion.
- **Context engineering practice** (Anthropic, Sept 2025, and the Claude API context-editing and compaction features):
  - Treat context as a finite budget.
  - Compact near the limit; tune summaries for recall before precision.
  - Clear stale tool results first; keep structured notes outside the window; let the agent retrieve on demand.
- **Mem0 / Zep-style memory:** explicit add, update, delete and no-op decisions against existing memories; bi-temporal validity (when a fact held vs when it was recorded).

**What follows for Hollis:**
- Lexical plus metadata retrieval is a strong, cheap baseline.
- Temporal validity and supersession must be explicit, not inferred from similarity.
- Agents should be able to retrieve on demand (a recall tool), not only through injected context.
- Long-context baselines are a real competitor, so the evaluation must compare against raw history.

## Design summary

**1. Context engine (`hollis.runtime.context`, provider-independent)**
- A request is assembled from typed parts:
  - *Mandatory:* instructions, input (the objective, never summarized), tool specs, the latest turn and the latest compaction summary.
  - *Optional:* recalled memories, older turns, older tool results.
- Recalled memories and summaries travel as structured `ContextBlock`s in `ModelRequest.context`. Adapters render them as data, never as instructions. History stays as structured assistant turns and tool results, with provider continuations intact.

**2. Token budget**
- Input budget = deployment `context_window_tokens` − reserved `max_output_tokens` − safety margin, further capped by a configured `max_input_tokens`.
- Estimation is a conservative character heuristic, calibrated upward from the run's own provider-reported usage.
- Exact provider counting (Anthropic `count_tokens`) is used only near the limit, through an optional `TokenCounter` port.
- Each call records the estimate, the method, the budget and the exclusions. If the budget is unknown, that is recorded too.

**3. Levers, cheapest and least lossy first**
1. Clear old tool results that are backed by an artifact (lossless: retrievable on demand).
2. Compact old turns.
3. Drop optional memories.
4. Fail with `context_exhausted` if the mandatory context alone doesn't fit. Never drop it silently.

Levers apply only under pressure, which keeps the provider's prompt-cache prefix stable.

**4. Durable and replayable**
- `MemoryRecalled`, `ContextCompacted` and `ContextAssembled` are events.
- A model request is a pure function of the state and the recorded plan, so retries of an operation send identical requests. The scripted provider's `OperationIdReused` check enforces this in tests.

**5. Compaction**
- Covers turns `[0, n)`, cut at turn boundaries so tool calls and results stay paired. The event log is never altered.
- The summary has two parts:
  - A *deterministic ledger* built from events: every side-effecting action, every error, read-only calls aggregated, and artifact references.
  - *Itemised notes* (decisions, observations, open questions, state), from a model-assisted summarizer through the gateway or from a deterministic fallback.
- Prior items carry forward unless the summarizer explicitly resolves them with a reason, so repeated compaction can't silently erode evidence.
- An invalid or failed summary falls back to the deterministic one.

**6. Artifacts**
- Full tool results above a threshold go to an `ArtifactStore` (in-memory or SQLite), scoped by run and content-addressed. The context gets a bounded preview plus a reference.
- Opt-in `artifact_read` and `artifact_search` tools read only the current run's artifacts, and frame content from external sources as untrusted.

**7. Memory (`hollis.memory`)**
- Typed records:
  - Kinds: fact, preference, procedure, episode, decision, observation.
  - Certainty: observed, inferred, confirmed.
  - Status: active, superseded, retracted, expired.
  - Subject and key slots, validity interval and record time, provenance with trust, conflict links.
- A governed write path: policy (kinds, sources, size, secret rejection, scope access), then deduplication, then a slot decision. The decision is new, update (old one superseded with a validity end), correction (old one retracted), temporary (time-bounded, doesn't replace) or conflict (a lower-certainty claim never overwrites a confirmed one; both are kept and linked).
- History is kept in an append-only change log. Deletion is a hard delete plus a content-free tombstone.

**8. Retrieval**
- Hybrid lexical, metadata and temporal retrieval:
  - BM25 (SQLite FTS5, or the same scoring in memory); validity filtered as of a time.
  - Subject boost and one-hop expansion; certainty and recency weights; preferences pinned.
  - Conflicts and supersession annotated.
- Two paths: recall at run start (recorded, so cache-stable) and an on-demand `memory_recall` tool.
- No embeddings or vector database in M4 (decided by the evidence above). The ranking interface stays open for M5.

**9. Scope and isolation**
- Namespaced scopes (`user:alice`, `agent:researcher`, `workspace:acme`, …), granted per run as `MemoryAccess(read, write)`, recorded in `RunCreated`, and enforced in every store query.
- The model can't widen its scope.
- Memory never feeds tool policy.

**10. Governance**
- Registered secrets and credential patterns are rejected on write.
- Recalled content is framed as data with provenance. External-source memories are capped at `observed`.
- Retention per kind and certainty; usage-based pruning never deletes confirmed memories.
- Memory writes by the model go through tools, so M3 policy and approval apply.
- Honest deletion: deleting a memory doesn't erase copies in run logs that recorded it.

**11. Evaluation (`tests/evals/`)**
- Deterministic, seeded multi-session agent trajectories with probes:
  - LongMemEval-style categories.
  - MemoryAgentBench competencies.
  - AMA-style tool-log evidence.
  - Isolation and privacy probes.
- Compared conditions: no memory, raw history, context-only and memory, at equal token budgets.
- Metrics: evidence recall, stale or unsupported recall, leakage, forgetting, tokens, latency, storage.
- Compaction fidelity under adversarial (lossy) summarizers.
- An opt-in live evaluation answers the probes with a real deployment.

## Out of scope (M5 or later)

- Document ingestion and general knowledge retrieval (RAG), embeddings and vector indexes, graph stores.
- Global model-driven consolidation, autonomous reflection loops.
- Deleting whole runs (ADR-0010's mechanism).

## Commits

1. This plan.
2. Context engine, budgeting, compaction and events (runtime and adapters).
3. Artifact store and artifact tools.
4. Memory system.
5. Evaluation suite.
6. Security and architecture tests.
7. ADRs and documentation.
