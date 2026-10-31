# Hollis

A model-agnostic, local-first platform for building, running and orchestrating autonomous agents, with a Python SDK first.

**Status: pre-alpha. M1 (runtime core) is complete.** See the [roadmap](docs/product/roadmap.md).

## What exists today

- **A single-turn runtime.** A run makes exactly one model call and then completes or fails. The runner drives a run to a terminal state, and the same code path resumes it after a crash.
- **Event-sourced run state.** Every run is an append-only, versioned event log. State is rebuilt by folding over those events, and invalid sequences are rejected loudly. Appends use optimistic concurrency.
- **Persistence.** There are two run stores behind one tested contract: an in-memory store and a SQLite store (standard-library `sqlite3`, schema migrations).
- **A scripted fake model provider.** It's deterministic and deduplicates by operation id, so runs and crash recovery can be tested without network access.

How model calls are guaranteed: a durably recorded response is never followed by another provider call, but a call whose outcome is unknown after a crash is retried. Exactly-once execution is not claimed ([ADR-0006](docs/decisions/0006-model-call-execution-semantics.md)).

## Current limitations

- No real model providers. Only the scripted fake exists.
- No tool execution, multi-turn runs, memory or retrieval, or multi-agent orchestration.
- No CLI or REST API beyond `hollis --version`.
- Single process, single active runner per run. SQLite data is not encrypted at rest.
- Run input, instructions, model responses and failure messages are stored **verbatim**. Nothing redacts secrets yet ([details](docs/product/requirements.md#data-persisted-in-m1-and-secret-handling)).

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.12.

```bash
make sync              # install locked dependencies
make check             # format check, lint, type check (strict), tests
uv run hollis --version
```

## Documentation

- [Vision and scope](docs/product/vision.md) · [Requirements](docs/product/requirements.md) · [Roadmap](docs/product/roadmap.md)
- [Architecture overview](docs/architecture/overview.md) · [Testing strategy](docs/architecture/testing.md)
- [Decision records](docs/decisions/) · [M1 plan](docs/plans/m1-runtime-core.md)
- [Development process](docs/development.md)
- [Research notes](docs/research/)
