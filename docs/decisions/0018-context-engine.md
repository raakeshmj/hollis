# ADR-0018: Context engine, token budgets and compaction

- Status: Accepted (M4)
- Date: 2026-10-31

## Context

Through M3, every model call resent the run's whole history, so long runs grew until the provider rejected them or `max_model_turns` stopped them. There was no token accounting before a call. Deployments didn't declare a context window.

Tokenizers differ by provider and model, and most OpenAI-compatible servers offer no counting endpoint. Anthropic's Messages API has `count_tokens`.

Long-running agents need history reduced deliberately. That reduction must not destroy evidence (actions taken, decisions, failures), must not silently drop the task itself, and must not undermine replay and retry. The event log is the source of truth (ADR-0005), and every attempt of a logical call must send the same request (ADR-0006).

## Decision

**Structured assembly, not a flattened prompt.**
- A request is built from typed parts:
  - instructions (the system prompt, never mixed with data);
  - the run input (the objective, sent verbatim and never summarized);
  - `ModelRequest.context` blocks (recalled memories and the latest compaction summary);
  - the history kept verbatim (assistant turns with tool calls and provider continuations intact, and tool results);
  - tool specs.
- Adapters render context blocks as framed reference data (`render_context`) in the first user message: a separate text block on Anthropic, a prefix on OpenAI-compatible endpoints. Closing tags inside content are neutralized.

**Budget.**
- `TextCapabilities.context_window_tokens`, declared per deployment: None means unknown.
- `ContextPolicy`, persisted in `RunCreated`.
- Input budget = window − reserved `max_output_tokens` − safety margin (10% of the window by default), capped by an optional `max_input_tokens`. With neither a window nor a cap, the budget is unknown, and the plan records that.

**Token accounting is explicit about what it knows.**
- `estimated`: a conservative character heuristic. Three ASCII characters or one non-ASCII character per token, plus per-message overhead. Provider continuations aren't counted.
- `calibrated`: the heuristic scaled up by the ratio of provider-reported total input tokens to the estimate for the previous turn of the same run. Never scaled down; capped at 10×.
- `counted`: the provider's tokenizer through an optional `TokenCounter` port (implemented by the Anthropic adapter with `count_tokens`). Used only when the estimate exceeds `verify_with_provider_at` (90% by default) of the budget. A failed count keeps the estimate.
- Actual usage stays what the provider reports after the call. `Usage.cache_included_in_input` makes the total comparable across protocols: OpenAI's `prompt_tokens` includes cached tokens; Anthropic's `input_tokens` doesn't.

**Levers, only under pressure, cheapest and least lossy first.** Below `compact_at` (80%) of the budget, history is left alone, so the provider's prompt-cache prefix stays stable.
1. Replace artifact-backed tool results of older turns with references. This is lossless: the artifact tools retrieve them (ADR-0019).
2. Compact older turns, keeping `keep_recent_turns` verbatim. If that's already the case, compact all but the latest turn.
3. Drop recalled memories, lowest ranked first, and report each drop.
4. If the mandatory context still doesn't fit, fail the run with `context_exhausted` before calling the model. Mandatory context is never silently dropped.

`max_history_turns` also triggers compaction by turn count, for deployments with an unknown window.

**Compaction is derived and recoverable.**
- `ContextCompacted` records a `CompactionSummary` of turns `[0, n)`, cut at turn boundaries so tool calls and results stay paired. The turns stay in the log.
- The summary has two parts:
  - **Ledger.** Every tool action is rebuilt from events whenever the summary is rendered: side effects, errors and unknown outcomes listed individually; successful read-only calls aggregated; artifact references. A summarizer never writes it, so repeated summarization can't erode it.
  - **Notes.** Itemized decisions, observations and open items, plus a state paragraph. Items are immutable. A later compaction can add items, or resolve existing ones by id with a reason. Items that aren't resolved are carried forward automatically.
- The fold rejects a summary that drops an active item without resolving it, a compaction that covers the latest turn, and a plan that doesn't start exactly where the latest summary ends.
- Summarizers (`Summarizer` protocol):
  - `DeterministicSummarizer` (default, no model).
  - `ModelSummarizer`, through the `ModelProvider` port, so any gateway deployment works, including a small local one.
  - Any failure or invalid draft falls back to the deterministic one, and the reason is recorded.
  - Registered secrets are redacted from summaries before persisting.

**Replayable plans.**
- `MemoryRecalled` (once, before the first call), `ContextCompacted` and `ContextAssembled` are events. `ContextAssembled` records history start, cleared results, included memories, estimate, budget and exclusions.
- A request is a pure function of state and the recorded plan, so every attempt of a logical call sends the same request. The scripted provider's `OperationIdReused` check enforces this in tests.
- Runs from before M4 (no plan) are sent with their full history.

## Alternatives considered

- **One tokenizer for all models** (e.g. tiktoken): wrong for most models and an extra dependency. It would also invite false precision.
- **Always count with the provider:** an extra round trip per call, and unavailable for most compatible servers.
- **Plain summary-of-summaries compaction:** in our evaluation it kept 17% of notes, where carry-forward kept 100% (docs/architecture/evaluation.md).
- **Summarizing the objective along with history:** the task drifts, so the input is mandatory and verbatim.
- **Sliding window without summaries:** simple, but silently drops early decisions and actions.
- **Clearing tool results on every turn by policy:** changes the prompt prefix every turn and defeats provider caching.
- **Rendering memories into the system prompt:** that makes data look like instructions.

## Trade-offs

- **The heuristic overestimates** for English prose, so compaction triggers earlier than strictly needed. Calibration corrects underestimates, but never overestimates.
- **Lost narrative:** the deterministic summarizer keeps actions and carried notes, but only the latest assistant message of the compacted range. Narrative reasoning in earlier turns is lost from context, though it stays in the log.
- **Model summarizer cost and duplicates:** its calls aren't recorded with an intent event. A crash during compaction repeats the call, at-least-once and at extra cost.
- **Poisoned notes:** a summarizer steered by injected tool output can write misleading notes. Notes are framed data and never feed authorization, and the ledger can't be forged by a summarizer.
- **Planning cost** grows with history length times the number of clearable results. That's acceptable at current scales but not optimized.

## Consequences

- **Evidence:**
  - `tests/runtime/test_context.py`, `test_context_fold.py`, `tests/adapters/test_context_rendering.py`, `tests/security/test_memory_security.py`, `tests/evals/`.
  - Mutation checks show that removing carry-forward, the history-start rule, the dropped-note rule, overflow failure, memory dropping, summary redaction or tag neutralization fails tests.
- **Not verified:** no live deployment was available. The heuristic's error against real tokenizers, and Anthropic `count_tokens`, are tested only offline.
- **Presets declare no context window:** windows are model-specific, so operators set `context_window_tokens` per deployment.
