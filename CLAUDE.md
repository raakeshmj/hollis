# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Hollis is a model-agnostic, local-first platform for building, running and orchestrating autonomous agents (Python SDK first, with a CLI and REST API later). The **runtime and harness** are the foundation. Coding agents are one specialization, so don't let the core assume coding tasks.

**Current milestone: M0 (foundation) is done; M1 (runtime core) is next.** Don't implement work from later milestones. See [docs/product/roadmap.md](docs/product/roadmap.md).

## Repository map

- `src/hollis/`: the package. Currently only the CLI entry point (`main`, `--version`).
- `tests/`: pytest suite.
- `docs/product/`: vision and non-goals, requirements (FR-x / NFR-x IDs), roadmap.
- `docs/architecture/`: `overview.md` (components, decided vs open), `testing.md` (test and eval strategy).
- `docs/decisions/`: ADRs. Use `0000-template.md` for new ones.
- `docs/plans/`: milestone implementation plans (start with `m1-runtime-core.md`).
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
- Use Pydantic v2 for validated domain and boundary types once they exist (M1+).
- Don't create packages or modules to reserve namespace. Create them when there is code for them.
- Don't add dependencies without a justification in the commit message. Architecture-shaping dependencies need an ADR.

## Testing expectations

- Every behavior change ships with a test. Test through public interfaces, and prefer shared fakes (fake provider, in-memory store) over mocks.
- The default suite never touches the network.
- Enforce important invariants with tests, not prose. Examples: import boundaries, secrets never persisted, illegal state transitions rejected.
- Run `make check` before declaring work done. Never report a check as passing without running it, and report actual results.

## Constraints

- Modular monolith. No microservices, distributed infrastructure, vector databases or model-training infrastructure.
- Local-first: everything must work on one machine. Storage sits behind a port (SQLite proposed, [ADR-0004](docs/decisions/0004-local-first-sqlite-persistence.md)).
- Treat model output as untrusted input. Secrets must never enter persisted events or logs.

## Workflow

- Use Plan Mode for complex or high-impact changes. Approved, bounded tasks go straight to implementation.
- Work on a branch, in small commits that each pass `make check`, with conventional prefixes (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`, `ci:`).
- Keep this file short (target ≤ 100 lines). Detailed guidance belongs in `docs/` or in path-scoped rules or skills, added only when a need is proven ([docs/development.md](docs/development.md)).
