# ADR-0007: Failure classification, Hollis-owned retries, ambiguous-outcome policy and budgets

- Status: Accepted (M2). Amends ADR-0006.
- Date: 2026-10-31

## Context

M1 had two provider outcomes: success, or `ModelProviderError` (a definitive rejection). Any other exception was ambiguous and retried on resume under the same operation id. That was safe only because the scripted fake deduplicates. Anthropic, the first real provider, documents no idempotency mechanism (ADR-0008). Blindly retrying an ambiguous outcome can therefore produce, and bill, a second generation.

Real providers also fail transiently (rate limits, overload). SDKs retry those internally and invisibly, which would multiply physical attempts behind Hollis's back. Real calls need bounded attempts and time (NFR-6).

## Decision

**Classification.**
- Providers raise `ModelCallError(kind, message, provider_request_id, retry_after_s)`.
- `FailureKind` is provider-neutral and determines two properties:
  - **Outcome:** `not_processed` (rejected, no generation) or `ambiguous` (may have generated).
  - **Retryability.**
- Unclassified exceptions still propagate, so bugs aren't swallowed. The run stays in flight.

| Kinds | Outcome | Retryable |
|---|---|---|
| authentication, permission, billing, invalid_request, quota_exhausted | not_processed | no |
| rate_limited, overloaded, unavailable, conflict, connection_failed | not_processed | yes |
| timeout, server_error, connection_lost, interrupted | ambiguous | per policy |

**Hollis owns retries.**
- Providers must make exactly one request per `generate` call. The Anthropic adapter sets `max_retries=0` on every call.
- Each attempt appends a `ModelCallStarted` intent. Every attempt then ends with a recorded outcome, `ModelResponseRecorded` or the new **`ModelCallFailed`**, before another may start. The fold enforces this.
- On resume, an intent with no outcome is recorded as `ModelCallFailed(interrupted)`, which is ambiguous.

**Ambiguous-outcome policy.**
- `AmbiguousOutcomePolicy` is `fail` or `retry`, recorded in `RunCreated`.
- The default comes from `ProviderCapabilities.deduplicates_requests`: `retry` if the provider deduplicates, otherwise **`fail`**.
- `retry` can be chosen explicitly, accepting the risk of a second charge.

**Budgets.**
- `RunBudget` is recorded in `RunCreated`, so recovery enforces the same limits:
  - `max_output_tokens`: sent to the provider.
  - `max_attempts`: physical attempts. The fold rejects attempts beyond it.
  - `attempt_timeout_s`: applied by the provider and also enforced by the runtime with `asyncio.timeout`.
  - `deadline_s`: wall clock from `RunCreated.occurred_at`, so it survives resume.
- **Retry delay:** the provider's `retry-after` is honoured exactly. Otherwise exponential backoff from 0.5 s to 8 s with equal jitter.
- A wait that would pass the deadline, or exceed 600 s, fails the run instead of sleeping.
- `RunFailed.reason` is one of `provider_error`, `ambiguous_outcome` or `budget_exhausted`.

**Event mapping.**
- Provider invocation: `ModelCallStarted`.
- Not accepted: `ModelCallFailed(not_processed)`.
- Successful response: the provider's result.
- Ambiguous outcome: `ModelCallFailed(ambiguous)`.
- Durably recorded response: `ModelResponseRecorded`.

**Pre-release schema baseline reset.**
- `RunCreated`, `ModelResponseRecorded` and `RunFailed` changed shape, so they're now schema version 2.
- Version 1 of those types was produced only by M1 development builds. It is rejected (`MIN_SUPPORTED_SCHEMA_VERSIONS`) rather than upcast.
- ADR-0005 makes the schema a contract *once released*; nothing has been released.

## Alternatives considered

- **Let the SDK retry**: attempts would be invisible and uncounted, and could multiply with Hollis retries.
- **Always retry ambiguous outcomes (M1 behaviour)**: silently risks duplicate generations and charges with real providers.
- **Always fail ambiguous outcomes**: needlessly fails runs with providers that do deduplicate. The capability-based default avoids that.
- **Treat 500 and 504 as not processed**: more runs would succeed, but the API docs describe 504 as "timed out while processing", so Hollis can't know nothing was generated.
- **Upcast M1 payloads**: maintenance cost for data that was never released.

## Trade-offs

- The conservative classification of 500/504 as ambiguous fails some runs that a retry would have rescued. That's a cost of not double-charging by default.
- `429` without `retry-after` is classified as quota exhaustion and is non-retryable. This follows the API docs' description of spend caps; if Anthropic changes that, the classification must follow.
- An extra event per failed attempt makes logs longer, but each one makes the run's history explicit.

## Consequences

- The ADR-0006 guarantees still hold: a recorded response is never followed by another invocation, at most one response is recorded per logical call, and there's no exactly-once claim. "Ambiguous outcome means retry" is now a policy choice, not a fixed rule.
- **Evidence:**
  - `tests/runtime/test_runner.py`: policy, budgets, deadline, retry-after.
  - `tests/recovery/`: crash boundaries including after `ModelCallFailed`, a non-deduplicating matrix, and Anthropic under both policies.
  - `tests/runtime/test_fold.py`: the tightened transition table.
- **Future:** cost budgets and input-token pre-estimation need reliable pricing data and `count_tokens`; neither is implemented. Multi-turn runs (M3) will extend `operation_id_for` with a call index.
