# ADR-0006: Model-call execution semantics and ambiguous outcomes

- Status: Accepted; amended by [ADR-0007](0007-failure-classification-retries-and-ambiguous-policy.md) (M2), which replaces "ambiguous outcome means retry" with an explicit, capability-based policy and adds `ModelCallFailed`.
- Date: 2026-10-31

## Context

A model call is an external side effect: it costs money and may not be repeatable. The runtime must recover from crashes (NFR-2) without silently duplicating or losing work (FR-4).

A crash can happen **after the provider has generated a response but before the runtime durably records it**. Afterwards, the local log shows an intent with no result. From the log alone, the runtime can't tell whether the provider ran, how many times, or whether it was billed. No local mechanism can make an external call exactly-once.

## Decision

- **Stable operation id.** Each logical model call has an operation id that's deterministic from the run (`{run_id}/model/{call_index}`; M1 makes one call, index 0). Every attempt, retry and recovery of that call sends the same id as `ModelRequest.operation_id`, the idempotency key.
- **Intent before every attempt.** The runtime appends `ModelCallStarted(operation_id, attempt=n)` before each physical invocation. Under optimistic concurrency, only one executor can win attempt *n*, so two executors never invoke for the same attempt number.
- **Record after success.** On success the runtime appends `ModelResponseRecorded(operation_id, attempt, response)`.
- **No reinvocation after recording.** Once a response is durably recorded, the provider is never invoked again for that call. Resume proceeds straight to completion.
- **Ambiguous outcome means retry under the same id.** An intent without a recorded response (a crash, or any exception other than `ModelProviderError`) leaves the run `model_call_in_flight`. Resume appends the next attempt intent and re-invokes with the same operation id.
- **Definitive rejection means failure.** `ModelProviderError` records `RunFailed`. It's terminal and not retried.

**Guarantees:**
1. At most one recorded response per logical call (fold plus OCC).
2. No invocation after a response is recorded.
3. At-least-once invocation.

**Not guaranteed:** exactly-once execution at the provider. That holds only if the provider deduplicates requests by the idempotency key. The scripted fake does; real providers may not.

**Observability:** four things are counted separately:
- Intents (`ModelCallStarted` events): attempts *initiated*. An intent may never reach the provider if the crash happens first.
- Physical invocations (what the provider received).
- Successful logical generations: responses the provider actually produced. The fake counts definitive rejections separately, and treats reuse of an operation id with a different request as an error.
- Recorded responses.

## Alternatives considered

- **Claim exactly-once**: false for any provider that doesn't deduplicate, and it would hide real double-billing risk.
- **Fail on any ambiguous outcome**: safe against duplicate cost, but it turns every crash mid-call into a failed run. It's kept as a future per-provider policy (see below).
- **A per-attempt operation id**: defeats provider-side deduplication.
- **Record the request instead of an intent**: the request is a pure function of `RunCreated` in M1. Recording the assembled request is deferred to M4 (context assembly).

## Trade-offs

- Retrying ambiguous outcomes favors completing runs over avoiding duplicate provider cost. That's right for M1's deterministic fake, but not automatically right for every real provider.
- The log over-counts attempts relative to invocations (an intent written, then a crash). It's documented and tested rather than hidden.

## Consequences

- **M2 must add:**
  - A provider capability declaring whether it deduplicates by idempotency key.
  - An explicit policy for ambiguous outcomes (`retry` vs `fail_on_ambiguous`), with limits on retries and resumes.
  - A decision on whether "outcome unknown" needs its own event.
- Evidence: `tests/recovery/test_crash_recovery.py` checks every crash boundary against both stores. Crashing between generation and recording yields 2 invocations, 1 successful generation, 0 rejections and 1 recorded response. The retry carries an identical request; a mismatch would raise `OperationIdReused`. A recorded response never reinvokes, even with a provider that would fail on any call.
