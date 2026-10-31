# ADR-0014: Tool runtime and durable tool lifecycle

- Status: Accepted (M3)
- Date: 2026-10-31

## Context

Up to M2, a run made one model call. Agents need to act: the model requests tools, Hollis runs them, and the results go back to the model, repeated until an answer. Tools have external side effects, so three things must hold even across crashes and adversarial model output: a model's tool call must not be able to authorize itself; a side effect must not be repeated blindly; and execution must be bounded.

Two facts shaped the design:
- **Anthropic** requires the assistant's content blocks, including thinking blocks with their signatures, to be sent back unchanged when continuing after tool use. Thinking is always on for current models.
- **OpenAI-compatible endpoints** carry tool calls as JSON strings that may not parse.

## Decision

**Provider-neutral types** (`hollis.runtime.conversation`, `hollis.runtime.tools`):
- `ToolCall` (call id, name, parsed arguments, raw arguments, parse error).
- `ToolSpec` (what the model sees).
- `ProviderContinuation` (opaque content that is echoed back unchanged).
- `AssistantTurn` / `ToolResultsTurn` history.
- `ToolDescriptor`: effect, idempotency, reconcilability, trust, isolation and timeout, declared by the code that registers the tool.

**The loop.** A run alternates model turns and tool phases:
- It ends with an answer, a failure, or a suspension awaiting approval.
- `operation_id` is per turn (`{run}/model/{n}`). Invocation ids are `{run}/turn/{n}/call/{i}`.
- M2's model-call semantics (intent, outcome, retries, ambiguous policy) apply to every turn unchanged.

**Durable lifecycle per tool call:**

```
ToolCallDecided (validation + policy; arguments recorded)
  -> ToolApprovalResolved   (only if the decision was require_approval)
  -> ToolExecutionStarted   (intent, before the tool runs; attempt n)
  -> ToolResultRecorded     (exactly one per call: success, error, invalid, denied,
                             timeout, ambiguous; what the model sees)
```

**The fold enforces, regardless of which code appends events:**
- Execution needs an `allow` decision, or an approval whose binding digest matches the recorded run, invocation, tool version and schema digest, and the exact arguments.
- Nothing in a turn executes while any approval in it is pending. A batch can't half-apply.
- A started invocation can be re-executed only if its tool is idempotent.
- Results must match what happened (no "success" for a call that never started, no "denied" for an allowed one).
- Only tools enabled for the run can be authorized. Per-run and per-turn tool-call limits apply.
- The next model turn requires every result.
- Completion requires that no tool calls are outstanding.

**Recovery.** An invocation that started without a result (crash or cancellation):
- is re-executed with the same invocation id if the tool is idempotent;
- otherwise is reconciled if the tool supports it;
- otherwise is recorded `ambiguous`, and the model is told the outcome is unknown.

**Exactly-once is not claimed.** Tools receive the invocation id as an idempotency key.

**Execution:**
- A batch consisting only of read-only tools runs concurrently, bounded by `max_parallel_tools`. Otherwise calls run sequentially in model order.
- Tool timeouts are capped by the run deadline. No tool starts after the deadline.
- A timeout or unexpected exception is recorded as `error`/`timeout` for read-only tools and `ambiguous` for side-effecting ones.

**Results:**
- Registered secrets are redacted.
- Output beyond `max_tool_output_chars` is truncated, with an explicit marker and the original size recorded.
- Results of `external` tools are framed as untrusted data when rendered for the model. Framing happens at render time, so replay is identical.

**Limits** live in `RunBudget`, persisted in `RunCreated`: model turns, tool calls per run and per turn, parallelism, output size, approval expiry, and the run deadline.

**Schema evolution:**
- `RunCreated` and `ModelResponseRecorded` move to v3 with upcasters from v2. The new fields have defaults; this is the first real use of ADR-0005's upcaster mechanism.
- New event types start at v1.

## Alternatives considered

- **Re-evaluating policy at execution time:** stronger against policy changes during a run, but it makes a recorded decision non-final and complicates audit. Rejected for M3 (see consequences).
- **Executing approved calls while others await approval:** lower latency, but it can half-apply a sequence of side effects.
- **Parallelism for any independent calls:** ordering of side effects matters, and dependency detection is guesswork.
- **Storing framed text:** framing would be frozen into history, and changing the framing would alter replays.
- **One combined "tool result" event without an intent:** loses the crash-ambiguity signal that recovery depends on.

## Trade-offs

- Strict sequencing of side effects and batch holding add latency.
- Recording validated arguments and results makes the event log larger. Size limits bound it.
- Framing is a mitigation for prompt injection, not a guarantee. Authorization is what actually prevents harm.

## Consequences

- **Known limitation:** an `allow` or approval recorded before a policy change still stands for that invocation.
- **Known limitation:** results above the size limit are truncated, with no artifact store for the full output yet.
- **Evidence:**
  - `tests/runtime/test_tool_loop.py`, `test_tool_fold.py`, `tests/recovery/test_tool_recovery.py`, `tests/security/`.
  - Mutation checks show that removing execution authorization, binding verification, batch holding, idempotent-only retry, the credential guard, framing or redaction fails tests.
