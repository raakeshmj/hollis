# Hollis

A model-agnostic, local-first platform for building, running and orchestrating autonomous agents, with a Python SDK first.

**Status: pre-alpha. M2 (first real model provider) is complete.** See the [roadmap](docs/product/roadmap.md).

## What exists today

- **Single-turn runtime.** A run makes one logical model call and then completes or fails. The runner drives a run to a terminal state; the same code path handles retries and resuming after a crash.
- **Event-sourced run state.** Every run is an append-only, versioned event log. State is rebuilt by folding over those events, invalid sequences are rejected loudly, and appends use optimistic concurrency.
- **Anthropic Claude provider** (`hollis[anthropic]`, official SDK). Captures usage, stop reason and the provider request id. Classifies failures from typed errors and status codes.
- **Explicit failure handling.**
  - Hollis owns all retries: each physical attempt is recorded, and the SDK's own retries are disabled.
  - Transient rejections (rate limits, overload) are retried within budget, honouring `retry-after`.
  - Outcomes that may have produced a billed generation (timeouts, server errors, crashes mid-call) are *ambiguous*. By default they are **not** retried for providers that can't deduplicate requests, which includes Anthropic ([ADR-0007](docs/decisions/0007-failure-classification-retries-and-ambiguous-policy.md)).
- **Budgets:** max attempts, max output tokens, per-attempt timeout, run deadline.
- **Credentials and redaction.** API keys load through a credential boundary and never enter run data. Known secrets and credential patterns are redacted from failure messages and logs ([ADR-0009](docs/decisions/0009-credentials-and-redaction.md)).
- **Persistence:** in-memory and SQLite stores behind one tested contract.
- **A scripted fake provider** for deterministic offline tests.

## Current limitations

- One provider (Anthropic) and non-streaming only. Output is capped at 21,333 tokens per response.
- No tool execution, multi-turn runs, memory or retrieval, or multi-agent orchestration. No CLI or REST API beyond `hollis --version`.
- No cost accounting or input-token budgets. Usage is recorded as reported by the provider.
- Run input and instructions are stored **verbatim**. Secrets embedded in user text are not detected unless they are registered values ([details](docs/product/requirements.md#data-persistence-and-secret-handling)).
- No retention or deletion mechanism yet (policy only, [ADR-0010](docs/decisions/0010-retention-and-deletion.md)). SQLite data is not encrypted at rest.
- Single process, single active runner per run.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.12. No API key is needed for development or tests.

```bash
make sync              # install locked dependencies (dev includes the anthropic extra)
make check             # format check, lint, type check (strict), tests, offline
uv run hollis --version
```

Opt-in live check against the real API (costs a fraction of a cent):

```bash
HOLLIS_LIVE_TESTS=1 ANTHROPIC_API_KEY=... uv run pytest tests/live -m live -ra
```

## Documentation

- [Vision and scope](docs/product/vision.md) · [Requirements](docs/product/requirements.md) · [Roadmap](docs/product/roadmap.md)
- [Architecture overview](docs/architecture/overview.md) · [Testing strategy](docs/architecture/testing.md)
- [Decision records](docs/decisions/) · Plans: [M1](docs/plans/m1-runtime-core.md), [M2](docs/plans/m2-anthropic.md)
- [Development process](docs/development.md)
- [Research notes](docs/research/)
