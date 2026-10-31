# M2 Plan: Real Model Integration (Anthropic)

Status: approved 2026-10-31.

## Context
M1 (branch `m1-runtime-core`, head `4da99a7`, clean) delivered a deterministic single-turn runtime. It assumes a provider that either succeeds, raises `ModelProviderError` (definitive), or raises anything else (ambiguous, blindly retried on resume with the same operation id). That blind retry was only safe because the scripted fake deduplicates.

M2 adds the first real provider, Anthropic Claude, through the official SDK. The runtime must not depend on that SDK. M2 also replaces the blind retry with explicit failure classification, Hollis-owned retries, an ambiguous-outcome policy, budgets, and credential handling with redaction. Branch: `m2-anthropic`, cut from `m1-runtime-core`.

## Research findings (primary sources, 2026-10-31)
Sources: the Claude API skill reference, `platform.claude.com/docs/en/api/errors`, and the `anthropic` **2.3.0** wheel from PyPI, read in the scratchpad and not executed.
- **SDK:** `anthropic` 2.3.0 (Python ≥ 3.10).
  - Depends on `httpx2` (not `httpx`), `opentelemetry-api`, `anyio`, `pydantic`, `jiter`.
  - The default header is `anthropic-version: 2026-10-21`. This API version rejects `temperature`/`top_p`/`top_k`; Hollis sends none.
- **No provider-side idempotency.** The SDK's base client has a generic idempotency-key hook, but `Anthropic`/`AsyncAnthropic` leave `_idempotency_header = None`, so no key is sent. The errors docs document no idempotency mechanism. Conclusion: Anthropic does **not** deduplicate, and Hollis's operation id gives no provider-side protection.
- **SDK retries (defaults):** `max_retries=2` on connection errors, 408, 409, 429 and ≥ 500. It honours `retry-after-ms`, `retry-after` and `x-should-retry`, with backoff from 0.5 s to 8 s. Timeout is 600 s overall and 5 s to connect. These retries are invisible to Hollis, so they'd multiply physical attempts.
- **Typed errors:**
  - Status errors: `BadRequestError` 400, `AuthenticationError` 401, `PermissionDeniedError` 403, `NotFoundError` 404, `ConflictError` 409, `RequestTooLargeError` 413, `UnprocessableEntityError` 422, `RateLimitError` 429, `InternalServerError` ≥ 500, `ServiceUnavailableError` 503, `DeadlineExceededError` 504, `OverloadedError` 529.
  - Connection errors: `APIConnectionError` and `APITimeoutError`, raised `from` the underlying `httpx2` exception, so connect-phase failures (`httpx2.ConnectError` / `ConnectTimeout`) can be identified by type.
  - `APIStatusError` exposes `.status_code`, `.request_id`, `.type` and `.response.headers`.
  - Per the docs, 402 is a billing error; an org spend limit returns **400**; a tier spend-cap 429 has **no** `retry-after`; 504 means "timed out while processing".
- **Response:** `Message.content` is a discriminated union. It includes `text`, `thinking` and `redacted_thinking`, plus tool and server-tool blocks that won't appear because Hollis sends no tools.
  - `stop_reason`: `end_turn | max_tokens | stop_sequence | tool_use | pause_turn | refusal | model_context_window_exceeded`.
  - `usage` has `input_tokens`, `output_tokens`, the optional cache fields, and `output_tokens_details.thinking_tokens`.
  - The request id is available as `message._request_id` (public despite the underscore) and in the `request-id` header.
- **Thinking and effort:**
  - On `claude-opus-5-5`, thinking is always on. `thinking: disabled` or `budget_tokens` is a **400**.
  - Effort goes in `output_config.effort` (`low` to `max`). The default is **`medium`** on Opus 5.5 and Haiku 5.5, and `high` elsewhere. Effort isn't supported on some older models.
  - Safe rule: **never send `thinking`**, which means each model's default. Send `output_config.effort` only when explicitly configured.
- **Non-streaming limit:** the SDK refuses non-streaming requests that are expected to exceed 10 minutes (`max_tokens > 21,333` at 128k tokens per hour), but only when no timeout is passed. Hollis passes timeouts, so it must enforce that bound itself.
- **Other SDK behaviour:**
  - When a tracer provider is registered, the SDK emits OpenTelemetry spans and propagates trace headers by default; it doesn't capture content.
  - With no explicit credential, it resolves credentials from the environment or profiles.
- **Pricing:** the only source is a cached price table in the skill. There's no pricing API, so no programmatic cost is computed (see D7).

## Architecture decisions
| # | Decision |
|---|---|
| **D1** | **Hollis owns retries.** The SDK client is built with `max_retries=0`. Every physical attempt is a durable `ModelCallStarted` intent, so intents = physical attempts. |
| **D2** | **Every attempt ends with a recorded outcome before any retry.** A new `ModelCallFailed(operation_id, attempt, failure)` event. The fold tightens: an in-flight attempt can only be followed by `ModelResponseRecorded` or `ModelCallFailed`. |
| **D3** | **Failure classification is a runtime type.** It's provider-neutral and lives in `hollis.runtime.model`. Each failure has a `kind` and an `outcome` (below). |
| **D4** | **Explicit ambiguous-outcome policy**, persisted in `RunCreated`: `fail` or `retry`. Default: `retry` only if the provider declares it deduplicates, otherwise **`fail`**. `retry` remains an explicit opt-in, and its double-charge risk is documented. |
| **D5** | **Provider capabilities** are explicit: `ProviderCapabilities(name, deduplicates_requests)`. Fake: `True`. Anthropic: `False`. The runner refuses to resume a run with a provider whose name differs from the one recorded. |
| **D6** | **Budgets** (`RunBudget`, persisted in `RunCreated`, validated by Pydantic): `max_attempts` (default 3), `max_output_tokens` (required, sent as `max_tokens`), `attempt_timeout_s` (default 120), `deadline_s` (optional wall clock from `RunCreated.occurred_at`, survives resume). The per-attempt timeout is the smaller of `attempt_timeout_s` and the time left before the deadline. |
| **D7** | **Usage, not cost.** Record provider-reported usage per response and accumulate actual usage in `RunState`. No built-in prices: cost and input-token budgets need pricing data or token estimates (`count_tokens`, an extra API call). Both are documented as future work. |
| **D8** | **Provider-specific options stay in the adapter.** `AnthropicSettings(effort: … \| None)` is configured on the provider instance, not forced into `ModelRequest`. `ModelRequest` gains only the generic `max_output_tokens` and `timeout_s`. |
| **D9** | **`anthropic` is an optional extra** (`hollis[anthropic]`). The core stays dependency-light (it would otherwise pull in httpx2, OpenTelemetry and more). `make sync` installs all extras, so dev and CI test the adapter. Importing `hollis`, `hollis.runtime` or the other adapters never imports the SDK (tested in a subprocess). |
| **D10** | **SDK OpenTelemetry is off** (`open_telemetry=False`). Trace attributes are a Hollis concern for later, so nothing leaves through SDK spans. Documented. |
| **D11** | **Credential boundary.** `load_anthropic_api_key()` reads `ANTHROPIC_API_KEY` into a `SecretStr`, or raises `MissingCredential` with a clear message, only when called. The key goes straight to the SDK client. It is never part of a `RunSpec`, `ModelRequest`, event or log. Callers who need profile or federation auth can inject a pre-built `AsyncAnthropic` client. |
| **D12** | **Redaction.** A `Redactor` in the runtime with: (a) exact known secret values (minimum length guard), and (b) a small set of credential-format patterns (`sk-ant-…`, `Bearer …`, `x-api-key: …`). Applied to provider failure messages, `RunFailed` messages and runtime log records. Known secret values are also redacted from response content. **Run input is never altered**, because that would break recovery (same operation id, same request). Instead, `Runner.create` **rejects** input or instructions containing a known secret value. Pattern-only matches in input aren't detected, a documented limitation. |
| **D13** | **Stop reasons map to a Hollis enum.** Refusals, `max_tokens` and context-window stops are *recorded responses* (they were generated and billed), not errors. The run completes, and `response.stop_reason` says how. Text blocks are joined in order; thinking blocks are ignored; types of unexpected non-text blocks are recorded in metadata. |
| **D14** | **Event-schema baseline reset, before release.** Changed payloads (`RunCreated`, `ModelResponseRecorded`, `RunFailed`) go to schema_version 2 with no upcasters. M1 logs exist only in tests and dev databases, so old logs fail loudly with `UnknownEventSchema`. This is stated in the ADR, consistent with ADR-0005 ("public contract once released"). |
| **D15** | **Retention and deletion (your choice):** a policy ADR (whole-run deletion, operator-configured retention) with the mechanism deferred and tracked explicitly. No store changes in M2. |

### Failure taxonomy (D3)
`outcome` is either `not_processed` (rejected; no generation, safe to retry) or `ambiguous` (the provider may have generated and charged).

| Kind | Source (Anthropic adapter, typed only) | Outcome | Retryable |
|---|---|---|---|
| `authentication` / `permission` / `billing` | 401 / 403 / 402 | not_processed | no |
| `invalid_request` | 400, 404, 413, 422; also local validation (e.g. `max_output_tokens` over the non-streaming bound) | not_processed | no |
| `rate_limited` | 429 **with** `retry-after` (the delay is captured) | not_processed | yes |
| `quota_exhausted` | 429 **without** `retry-after` (the documented spend-cap signature) | not_processed | no |
| `overloaded` / `unavailable` / `conflict` | 529 / 503 / 409 | not_processed | yes |
| `connection_failed` | `APIConnectionError` caused by `httpx2.ConnectError` or `ConnectTimeout` | not_processed | yes |
| `timeout` | other `APITimeoutError` (read or write) | ambiguous | per policy |
| `server_error` | 500, 504, other 5xx | ambiguous | per policy |
| `connection_lost` | other `APIConnectionError` | ambiguous | per policy |
| `interrupted` | inferred by the runner on resume: intent with no outcome (crash, cancellation, an unclassified exception) | ambiguous | per policy |

`ModelCallError(kind, message, provider_request_id, retry_after_s)` replaces `ModelProviderError`. `outcome` is derived from `kind`. Any unclassified exception still propagates, as in M1, so bugs aren't swallowed. The run stays in flight and resume classifies it as `interrupted`.

### Run lifecycle (runner loop; first execution and recovery share one path)
`pending → model_call_in_flight → {model_responded → completed | attempt_failed → (model_call_in_flight | failed)}`
- **`model_call_in_flight` on resume:** append `ModelCallFailed(interrupted, ambiguous)`, then decide.
- **`attempt_failed`:** the decision depends on the outcome.
  - Not retryable: `RunFailed(provider_error)`.
  - Ambiguous under the `fail` policy: `RunFailed(ambiguous_outcome)`.
  - Otherwise, if attempts and deadline allow: wait (`retry_after` if present, otherwise capped exponential backoff with jitter), then the next intent.
  - Else `RunFailed(budget_exhausted)`.
  - The sleep, clock and RNG are injected, so tests are deterministic.
- **`model_responded`:** append `RunCompleted`. **The provider is never invoked again (M1 guarantee kept).**
- **What maps to what:**
  - Provider invocation = `ModelCallStarted`.
  - Not accepted = `ModelCallFailed(not_processed)`.
  - Successful response = provider result.
  - Ambiguous outcome = `ModelCallFailed(ambiguous)`.
  - Durably recorded response = `ModelResponseRecorded`.

## Contract changes (smallest justified)
- **`hollis.runtime.model`:**
  - `ModelRequest` gains `max_output_tokens` and `timeout_s`.
  - `ModelResponse` gains `stop_reason`, `provider_request_id`, `served_model` and richer `Usage` (cache read/write, optional thinking tokens).
  - `ModelProviderError` is replaced by `ModelCallError`.
  - New: `FailureKind`, `CallOutcome`, `ProviderCapabilities`, and a `capabilities` member on the `ModelProvider` protocol.
- **`hollis.runtime.events`:**
  - `RunCreated` v2: adds `provider`, `budget`, `ambiguous_policy`.
  - New `ModelCallFailed` v1.
  - `ModelResponseRecorded` v2.
  - `RunFailed` v2: `reason` (`provider_error | ambiguous_outcome | budget_exhausted`), `kind`, sanitized `message`, `provider_request_id`, `attempts`.
- **`hollis.runtime.state`:** new status `attempt_failed`, a tightened transition table, `last_failure`, cumulative `usage`.
- **`hollis.runtime.runner`:** the retry loop, policy, budgets, deadline, redaction, provider-name check, and `logging` (redacted).
- **New `hollis.runtime.redaction`:** `Redactor`.
- **`ScriptedModelProvider`:** scripts `ModelResponse | ModelCallError`, keeps its request-aware deduplication, and declares `deduplicates_requests=True`.
- Stores are unchanged. The SQLite schema is unchanged, since events are opaque JSON.

## Anthropic adapter (`src/hollis/adapters/anthropic_provider.py`)
- **Construction:**
  - `AnthropicProvider(client: AsyncAnthropic, settings)`.
  - `AnthropicProvider.from_api_key(api_key: SecretStr, settings, *, http_client=None)` builds `AsyncAnthropic(api_key=…, max_retries=0, open_telemetry=False, http_client=…)`.
  - Supports `async with` / `aclose()`.
- **Request:**
  - `messages=[{"role": "user", "content": input}]`, `system` (only if present), `model`, `max_tokens`.
  - `output_config.effort` only if configured.
  - Never `thinking` or sampling parameters.
  - Per-attempt timeout via `client.with_options(timeout=…)`.
  - Validates `max_output_tokens` against the non-streaming bound before sending.
- **Response:** maps text, stop reason, usage, `_request_id` and the served model. SDK types never leave the module.
- **Errors:** classified by SDK exception class and status code, plus `retry-after-ms`/`retry-after` parsed from headers, and the `httpx2` cause type for connection errors. Never by message strings. Messages are built from safe fields (status, `error.type`, request id, then the provider message truncated and redacted).

## Testing (offline by default; no API key, no network)
- **Adapter tests:** a real `AsyncAnthropic` wired to an `httpx2.MockTransport` through the public `http_client` argument. No patching of SDK internals.
  - Fixtures are realistic JSON payloads following the current schema: text, thinking plus text, refusal with `stop_details`, `max_tokens`, error bodies with `request_id`.
  - Request construction: body fields, no `thinking` or sampling parameters, effort on or off, `system`, `max_tokens`; the key sent only in the `x-api-key` header.
  - Response parsing, usage, request id, stop-reason mapping, multiple blocks.
  - Classification for every row of the taxonomy (status codes, `retry-after` present/absent/ms, `ConnectError`, `ReadTimeout`, a mid-response disconnect). SDK retries are confirmed off (one HTTP call per attempt).
  - Missing credential, and sanitized messages containing no key.
- **Runtime tests (fake and Anthropic-with-mock):**
  - Retry on not_processed (honouring `retry_after`, deterministic sleeps).
  - `max_attempts` and deadline exhaustion; non-retryable failure goes straight to failed.
  - Ambiguous under `fail`: failed, exactly one HTTP call. Ambiguous under `retry`: a second call.
  - Policy default resolved from capabilities.
  - Provider-name mismatch rejected; secret in input rejected; usage accumulated.
- **Recovery matrix:** the M1 crash boundaries are kept and extended with "after `ModelCallFailed` commits". Covered with the non-deduplicating Anthropic mock, under both policies, on both stores. The M1 guarantee is asserted: a recorded response means zero further HTTP calls.
- **Redaction:**
  - Unit tests: known values, patterns, overlapping matches, minimum length.
  - End-to-end: a known key planted in provider error bodies and in response content never appears in serialized events, the SQLite file bytes, or captured log records (`caplog`).
- **Architecture:** the existing allowlist test still passes. Plus a subprocess test that importing `hollis`, `hollis.runtime` or the non-Anthropic adapters doesn't load `anthropic`.
- **Live test:** `tests/live/test_anthropic_live.py`.
  - Skipped unless both `HOLLIS_LIVE_TESTS=1` and `ANTHROPIC_API_KEY` are set.
  - Model from `HOLLIS_LIVE_MODEL` (default `claude-opus-5-5`), `effort=low`, `max_output_tokens=256`, one request.
  - Its docstring states it costs real money (fractions of a cent) and is the only check of live account access.

## Docs and ADRs
- **ADR-0007:** model-call failure classification, Hollis-owned retries, ambiguous-outcome policy and budgets. It **amends ADR-0006**, which gets an "Amended by" status: the M1 default of blind retry is replaced by the capability-based default.
- **ADR-0008:** Anthropic adapter. Optional extra, SDK configuration (`max_retries=0`, OpenTelemetry off), no idempotency, effort handling, non-streaming bound.
- **ADR-0009:** credentials boundary and redaction strategy, with limitations.
- **ADR-0010:** retention and deletion policy, mechanism deferred.
- **Updates:**
  - `requirements.md`: the secret-handling section rewritten to give M2 status per item (references, redaction and sanitization, and tests done; retention as policy only). NFR-4 and NFR-6 status.
  - Overview, testing.md, roadmap (M2 done; M3 next).
  - `CLAUDE.md`, README, `.env.example` (variable names only), M2 plan doc in `docs/plans/`.

## Commit sequence (each passes `make check`)
1. `docs: M2 plan` (this plan into `docs/plans/m2-anthropic.md`).
2. `feat(runtime): failure classification, capabilities and budgets in the model port`, including fake-provider updates.
3. `feat(runtime): ModelCallFailed event, attempt_failed state and schema v2 baseline`.
4. `feat(runtime): runner retries, ambiguous-outcome policy, deadline and usage accounting`.
5. `feat(runtime): redaction and secret-in-input rejection`.
6. `feat(adapters): Anthropic provider` (optional extra, credential loader, mock-transport tests).
7. `test: recovery matrix with non-deduplicating provider; import-isolation test; live opt-in test`.
8. `docs: ADR-0007..0010, requirements, architecture, roadmap, README, CLAUDE.md, .env.example`.

## Acceptance criteria / verification
- `make sync && make check` passes with no `ANTHROPIC_API_KEY` set and no network access. To check offline behaviour, run the suite with the key unset. The test transport makes outbound calls impossible.
- Pyright strict reports 0 errors. SDK types are confined to `anthropic_provider.py`. The architecture tests pass, including SDK import isolation.
- Every taxonomy row, the policy defaults, budget exhaustion, the deadline, retry-after handling, and "no reinvocation after a recorded response" are covered by tests. The new tests are mutation-checked: removing the policy check or setting `max_retries` back to the default must fail them.
- The planted-secret tests prove the key never appears in events, SQLite bytes or logs.
- The live test is skipped by default (shown as skipped in the `-ra` summary).
- **Honesty:** the report will state that mocked tests don't verify live account access or real provider behaviour, and that the live test hasn't been run (no key available).

## Non-goals
Other providers, routing or fallback, streaming, tools, multi-turn execution, RAG or memory, multi-agent, CLI or API surfaces, a dashboard, distributed execution, cost computation, input-token pre-estimation, a deletion mechanism, Hollis OpenTelemetry, refusal fallbacks (the server-side `fallbacks` beta).

## Risks and open points
- **The schema v2 reset breaks M1 dev databases** (by design, and loudly). This is acceptable only because nothing is released.
- **Classification heuristics are explicit choices, not guarantees.** 500/504 treated as ambiguous is conservative and may fail some runs that could have retried safely. 429 without `retry-after` treated as quota exhaustion follows the docs, but the docs could change.
- **`httpx2.MockTransport`** availability will be confirmed at install. Fallback: a minimal public `httpx2.AsyncBaseTransport` subclass.
- **Anthropic may add idempotency support later.** Capabilities make that a one-line change in the adapter, plus a test.

## Implementation notes (deviations and findings)

- **Commits 2–4 landed as one commit.** Replacing `ModelProviderError`, adding `ModelCallFailed` and the runner's retry loop can't pass `make check` independently.
- **SDK 2.3.0 sends API keys as `Authorization: Bearer`,** not `x-api-key`. The adapter test asserts the actual header.
- **The SDK's status mapping doesn't use its own `ServiceUnavailableError`/`DeadlineExceededError` classes.** 503 and 504 arrive as `InternalServerError`, and 402 as plain `APIStatusError`. Classification therefore uses the typed `status_code` attribute.
- **The dev group depends on `hollis[anthropic]`,** so `make sync` and CI always install the SDK, and adapter tests can't be skipped by a missing extra.
- **The fake provider gained `per_attempt=True`** (consume a new scripted failure on each attempt) and ignores `timeout_s` when comparing requests, because attempts of one call legitimately carry shorter timeouts as the deadline approaches.
- **A closed Anthropic client surfaces as `connection_lost` (ambiguous).** The SDK wraps the "client closed" error as a connection error. Conservative classification was kept rather than special-casing.
- **Verification:** the full suite passed with `ANTHROPIC_API_KEY` unset and outbound network denied by a macOS sandbox profile. The live test has **not** been run (no key available).
