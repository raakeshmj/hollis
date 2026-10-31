# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Hollis is a model-agnostic, local-first platform for building, running and orchestrating autonomous agents (Python SDK first, with a CLI and REST API later). The **runtime and harness** are the foundation. Coding agents are one specialization, so don't let the core assume coding tasks.

**Current milestone: M0 and M1 (single-turn runtime core) are done; M2 (first real provider) is next and not yet planned.** Don't implement work from later milestones. See [docs/product/roadmap.md](docs/product/roadmap.md).

## Repository map

- `src/hollis/runtime/`: the core. Events and schema registry, state fold, RunStore and ModelProvider ports, `Runner`. It imports only stdlib, pydantic and itself (enforced by `tests/architecture/`).
- `src/hollis/adapters/`: in-memory and SQLite stores, scripted fake provider.
- `tests/`: `runtime/`, `adapters/` (the shared store contract), `recovery/` (crash injection), `architecture/`. Shared builders and strategies live in `tests/support/`.
- `docs/product/`: vision and non-goals, requirements (FR-x / NFR-x IDs), roadmap.
- `docs/architecture/`: `overview.md` (components, decided vs open), `testing.md` (test and eval strategy).
- `docs/decisions/`: ADRs. Use `0000-template.md` for new ones.
- `docs/plans/`: milestone implementation plans (`m1-runtime-core.md` is the most recent).
- `docs/research/`: research notes with adopt now / later / no verdicts.
- `docs/development.md`: workflow, ADR policy, when to add skills, rules, hooks or subagents.

**Before making an architectural decision**, read `docs/architecture/overview.md` and the relevant ADRs. If the change is load-bearing and costly to reverse, propose a new ADR rather than deciding silently.

## Commands

```bash
make sync                    # uv sync --locked
make check                   # ruff format --check, ruff check, pyright (strict), pytest; CI runs this
make fix                     # ruff format + ruff check --fix
uv run pytest tests/test_cli.py::test_version_flag_prints_version_and_exits_zero   # single test
uv add <pkg> / uv add --dev <pkg>   # updates uv.lock (commit it)
```

## Conventions

- Python ≥ 3.12. Full type annotations; pyright strict must report 0 errors.
- Ruff rule set and line length (100) are configured in `pyproject.toml`. Don't add `noqa` or `type: ignore` without a reason on the same line.
- Use async only on I/O-bound paths (model, storage, tool execution); keep pure logic synchronous ([ADR-0003](docs/decisions/0003-async-first-runtime.md)).
- The core never imports vendor SDKs or adapters. Vendor code lives in adapters behind ports ([ADR-0002](docs/decisions/0002-ports-and-adapters.md)).
- Domain and event types are frozen Pydantic v2 models with `extra="forbid"`.
- Runtime invariants:
  - State comes only from `fold` over events.
  - Validate every new event with `apply` before appending it.
  - Never rewrite stored events. A payload shape change means a version bump plus an upcaster.
  - Model-call semantics follow [ADR-0006](docs/decisions/0006-model-call-execution-semantics.md): no exactly-once claims.
- Don't create packages or modules to reserve namespace. Create them when there is code for them.
- Don't add dependencies without a justification in the commit message. Architecture-shaping dependencies need an ADR.

## Testing expectations

- Every behavior change ships with a test. Test through public interfaces, and prefer shared fakes (fake provider, in-memory store) over mocks.
- The default suite never touches the network.
- Enforce important invariants with tests, not prose. Examples: import boundaries, illegal state transitions rejected.
- Run `make check` before declaring work done. Never report a check as passing without running it, and report actual results.

## Constraints

- Modular monolith. No microservices, distributed infrastructure, vector databases or model-training infrastructure.
- Local-first: everything must work on one machine. Storage sits behind a port. SQLite is the store, assuming a single process and a single active runner ([ADR-0004](docs/decisions/0004-local-first-sqlite-persistence.md)).
- Treat model output as untrusted input.
- Never put credentials into requests, events or logs.
- M1 persists input, instructions, responses and failure messages verbatim, with no redaction. Don't claim otherwise, and don't add real providers until the M2 secret-handling requirements are met ([docs/product/requirements.md](docs/product/requirements.md#data-persisted-in-m1-and-secret-handling)).

## Workflow

- Use Plan Mode for complex or high-impact changes. Approved, bounded tasks go straight to implementation.
- Work on a branch, in small commits that each pass `make check`, with conventional prefixes (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`, `ci:`).
- Keep this file short (target ≤ 100 lines). Detailed guidance belongs in `docs/` or in path-scoped rules or skills, added only when a need is proven ([docs/development.md](docs/development.md)).
