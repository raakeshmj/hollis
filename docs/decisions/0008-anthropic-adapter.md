# ADR-0008: Anthropic adapter

- Status: Accepted (M2)
- Date: 2026-10-31

## Context

M2 needs a first real model provider without making the runtime depend on any vendor SDK (ADR-0002). Facts were checked against the `anthropic` 2.3.0 wheel and the API errors documentation on 2026-10-31:
- **Idempotency:** the SDK sends no idempotency key (`_idempotency_header` is unset), and the Messages API documents no idempotency mechanism.
- **Default retries:** the SDK retries connection errors, 408, 409, 429 and 5xx twice, honouring `retry-after-ms`, `retry-after` and `x-should-retry`.
- **Status mapping:** it raises typed errors per status. 402, 503 and 504 arrive as generic `APIStatusError`/`InternalServerError` (the dedicated classes exist but aren't used by the status mapping). Connection errors chain the underlying `httpx2` exception.
- **Auth and version:** API keys are sent as `Authorization: Bearer`. The default API version is `2026-10-21`, which rejects sampling parameters.
- **Thinking:** on current models (e.g. `claude-opus-5-5`) thinking is always on, and sending `thinking: disabled` or `budget_tokens` is a 400. Effort (`output_config.effort`) is model-dependent.
- **Non-streaming bound:** the SDK refuses non-streaming requests expected to exceed 10 minutes (`max_tokens > 21,333`), but only when no explicit timeout is passed.
- **Telemetry:** the SDK emits OpenTelemetry spans and propagates trace headers by default when a tracer provider is registered.

## Decision

- `hollis.adapters.anthropic_provider.AnthropicProvider` implements `ModelProvider` with `AsyncAnthropic`. SDK types never leave the module.
- **Optional dependency.** It's packaged as the `hollis[anthropic]` extra. The dev group depends on `hollis[anthropic]`, so adapter tests always run. A subprocess test proves the core and other adapters never import the SDK, `httpx2` or `opentelemetry`.
- **Capabilities:** `name="anthropic"`, `deduplicates_requests=False`. The default ambiguous policy is therefore `fail` (ADR-0007).
- **No SDK retries:** `max_retries=0` and the per-attempt timeout are applied to every call via `with_options`, including on injected clients.
- **Request:**
  - `model`, `max_tokens` (from `RunBudget`), a single user message, optional `system`.
  - `output_config.effort` only when `AnthropicSettings.effort` is set. Effort is configured on the provider instance, not in the provider-neutral request.
  - `thinking` and sampling parameters are never sent.
  - `max_output_tokens` above 21,333 is rejected locally as `invalid_request`, because streaming is out of scope.
- **Response mapping:**
  - Text blocks are joined in order. `thinking` and `redacted_thinking` blocks are ignored. Other block types are listed in `omitted_block_types`.
  - Stop reasons map to Hollis's `StopReason`. Refusal, `max_tokens` and context-window stops are recorded responses, not errors.
  - Captured: usage (including cache and thinking tokens), the served model, and `_request_id`.
- **Classification:** uses the HTTP status code, `retry-after-ms`/`retry-after`, and the `httpx2` cause type (connect, connect-timeout or pool-timeout means not sent; anything else after sending is ambiguous). Never message text. Messages are built from the status, the error type and the API's `error.message` truncated to 300 characters. The runtime redacts them.
- **Telemetry and credentials:** clients built by the adapter set `open_telemetry=False`. `from_env()` reads `ANTHROPIC_API_KEY` through the credential boundary (ADR-0009). Callers needing profile or federation auth can pass their own client.

## Alternatives considered

- **Raw HTTP:** duplicates typed errors, auth and versioning that the official SDK maintains.
- **SDK as a core dependency:** forces httpx2 and OpenTelemetry on every user, including those using other providers.
- **Sending `thinking: {type: adaptive}` explicitly:** redundant on current models and rejected by models that only support extended thinking. Omitting it lets each model use its default.
- **Putting effort in `ModelRequest`:** it's Anthropic vocabulary. Adding it would start a lowest-common-denominator request type.
- **Streaming:** needed for outputs above the non-streaming bound. Deferred; the bound is enforced instead.

## Trade-offs

- Classifying on status codes depends on Anthropic keeping its documented semantics (for example 429 without `retry-after` meaning a spend cap).
- Effort per provider instance means one effort level per provider object. Per-run effort would need a typed provider-options mechanism, deferred until a second provider shows what's actually common.
- The non-streaming bound caps a single response at about 21k output tokens until streaming lands.

## Consequences

- Offline tests drive a real SDK client over `httpx2.MockTransport` with realistic payloads. They verify request construction, mapping and classification. They **don't** verify live account access or real provider behaviour. `tests/live/test_anthropic_live.py` is opt-in (`HOLLIS_LIVE_TESTS=1` plus a key) and costs money.
- If Anthropic adds request idempotency, the change is the capability flag plus sending the operation id as the documented key, with a test.
