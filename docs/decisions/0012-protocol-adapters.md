# ADR-0012: Protocol adapters, and no third-party multi-provider library

- Status: Accepted (M2)
- Date: 2026-10-31

## Context

Hollis needs adapters that preserve provider features, classify failures precisely for its retry and idempotency semantics, work with keyless local servers, and never route traffic implicitly. Two kinds are needed:
- **Native provider adapters**, for provider-specific APIs.
- **Protocol adapters**, reused across every server that implements a protocol.

Evidence gathered on 2026-10-31:
- **LiteLLM 1.106:** 23 core dependencies (boto3, tokenizers, huggingface-hub, aiohttp, …), pins `openai<3`, and has its own retries, fallbacks and routing.
- **any-llm 1.41:** pins `anthropic<1`, which conflicts with the `anthropic` 2.x adapter.
- **`openai` 3.42:** built on `httpx2` (the same stack as `anthropic` 2.x), but it requires a credential and reads `OPENAI_API_KEY`, `OPENAI_ORG_ID` and `OPENAI_PROJECT_ID` from the environment.
- **Server documentation shows real protocol variation:**
  - Ollama documents `max_tokens` but not `max_completion_tokens`, and maps `reasoning_effort` lossily per model. OpenAI has deprecated `max_tokens`.
  - Reasoning output appears in `reasoning_content` (vLLM, SGLang, llama.cpp).
  - Usage may be missing or approximate (Ollama's Messages API).

## Decision

- **Own a small adapter layer. Adopt no multi-provider library.** The libraries above conflict with Hollis's dependencies, duplicate its retry and fallback policy (which it must own), and normalize toward OpenAI's shape.
- **`anthropic-messages`** (`hollis.adapters.anthropic_messages`), on the official `anthropic` SDK. It serves Anthropic and Messages-compatible endpoints such as vLLM, llama.cpp, Ollama and LM Studio.
  - Explicit base URL. Auth is none, bearer or header. Version override available.
  - `ANTHROPIC_CUSTOM_HEADERS` is neutralized with the SDK's `Omit`. The SDK's credential and base-URL fallbacks are never reached.
- **`openai-chat-completions`** (`hollis.adapters.openai_chat`), on **raw `httpx2`**.
  - Rationale: exact control of what goes on the wire, keyless operation, no environment reads, and a small protocol surface for non-streaming text.
  - Configurable `token_limit_param`, `system_role`, `reasoning_effort` and operator `extra_body`. `extra_body` can't override managed fields.
  - Tolerant parsing. A 2xx response without a usable choice is `invalid_response`, which is ambiguous.
- **Shared plumbing** (`hollis.adapters._http`):
  - A client factory: `trust_env=False`, no redirects, custom CA and client certificate.
  - Transport classification: connect-phase failures are not processed; anything after sending is ambiguous.
  - Status classification, error-body parsing, retry-after parsing.
- **Per-endpoint quirks are options, not code branches.** For example, Anthropic's "429 without retry-after is a spend cap" applies only when `quota_on_429_without_retry_after` is set; the `anthropic` preset sets it.
- **Native OpenAI support,** when added, will be a separate adapter on the official SDK and the Responses API, which OpenAI recommends for new work.

## Alternatives considered

- **LiteLLM or any-llm:** rejected for the reasons above.
- **The `openai` SDK for the OpenAI-compatible protocol:** viable, but its credential requirement, environment fallbacks and OpenAI-platform orientation work against keyless and on-prem use.
- **Hand-rolled HTTP for Anthropic:** the official SDK is already a dependency and tracks the API.

## Trade-offs

- Owning the OpenAI-compatible wire code means owning SSE parsing when streaming arrives.
- Error shapes differ across compatible servers, so classification for them is less precise than for first-party APIs.
- Presets need upkeep as servers change.

## Consequences

- One adapter per protocol serves many servers. A new server that implements a supported protocol needs configuration, not code.
- **Evidence:**
  - `tests/adapters/test_openai_chat.py`, `test_anthropic_provider.py` (including hostile-environment tests).
  - `tests/network/test_loopback.py` (real sockets, TLS).
  - `tests/conformance/` (offline, plus opt-in live).
