# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Hollis is a model-agnostic, local-first platform for building, running and orchestrating autonomous agents (Python SDK first, with a CLI and REST API later). The **runtime and harness** are the foundation. Coding agents are one specialization, so don't let the core assume coding tasks.

**Current milestone: M3 (tool runtime) is implemented and verified offline, on top of M2 (model gateway); no live endpoint or third-party MCP server has been tested, and no sandbox exists. M4 (context and memory) is not yet planned.** Don't implement work from later milestones. See [docs/product/roadmap.md](docs/product/roadmap.md).

## Repository map

- `src/hollis/runtime/`: the core. Events and schema registry, state fold, RunStore and ModelProvider ports, the tool port (`tools.py`: descriptors, policy, approval), conversation history, result handling, failure taxonomy, budgets, `Redactor`, `Runner`. It imports only stdlib, pydantic and itself (enforced by `tests/architecture/`).
- `src/hollis/gateway/`: deployment catalog (Python or TOML), presets, routing policy, `ModelGateway` (implements the runtime's `ModelResolver`).
- `src/hollis/tools/`: `@tool` function tools, `ToolRegistry`, `RulePolicy`, built-in filesystem and command tools (`builtin/`), MCP client (`mcp.py`, optional extra).
- `src/hollis/adapters/`: stores, fake provider, credentials, endpoint config (`config.py`, SDK-free), shared HTTP layer (`_http.py`), protocol adapters `anthropic_messages.py` (official SDK) and `openai_chat.py` (raw httpx2). Provider SDKs and httpx2 are optional extras, loaded only by their adapter (enforced by subprocess tests).
- `tests/`: `runtime/`, `tools/`, `security/` (agentic attack scenarios), `adapters/` (mock transports), `gateway/`, `network/` (real loopback sockets and TLS), `conformance/` (offline, plus opt-in live via `HOLLIS_CONFORMANCE_*`), `recovery/`, `architecture/`, `live/`. Shared helpers live in `tests/support/`.
- `docs/product/`: vision and non-goals, requirements (FR-x / NFR-x IDs), roadmap.
- `docs/architecture/`: `overview.md` (components, decided vs open), `testing.md` (test and eval strategy).
- `docs/decisions/`: ADRs. Use `0000-template.md` for new ones.
- `docs/plans/`: milestone implementation plans (`m3-tools.md` is the most recent).
- `docs/research/`: research notes with adopt now / later / no verdicts.
- `docs/development.md`: workflow, ADR policy, when to add skills, rules, hooks or subagents.

**Before making an architectural decision**, read `docs/architecture/overview.md` and the relevant ADRs. If the change is load-bearing and costly to reverse, propose a new ADR rather than deciding silently.

## Commands

```bash
make sync                    # uv sync --locked (dev includes all provider extras)
make check                   # ruff format --check, ruff check, pyright (strict), pytest; CI runs this
make fix                     # ruff format + ruff check --fix
uv run pytest tests/test_cli.py::test_version_flag_prints_version_and_exits_zero   # single test
uv add <pkg> / uv add --dev <pkg>   # updates uv.lock (commit it)
HOLLIS_LIVE_TESTS=1 ANTHROPIC_API_KEY=... uv run pytest tests/live -m live   # opt-in, costs money
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
  - Model-call semantics follow ADR-0006 as amended by [ADR-0007](docs/decisions/0007-failure-classification-retries-and-ambiguous-policy.md): every attempt gets an intent and a recorded outcome; Hollis owns retries (providers make one request per call); ambiguous outcomes follow the run's policy; no exactly-once claims.
  - Providers raise `ModelCallError` with a `FailureKind`, classified from typed errors, status codes and transport types, never from message text.
- Model architecture ([ADR-0011](docs/decisions/0011-model-gateway-and-deployments.md)–[0013](docs/decisions/0013-locality-routing-and-on-prem.md)):
  - Runs reference logical deployments. The protocol selects the adapter; the provider label never does.
  - New servers that implement a supported protocol need configuration, not code.
  - Capabilities are declared per deployment and validated before persistence. Never silently drop unsupported parameters.
  - Never add implicit routing: explicit base URL and auth, no SDK environment fallbacks, no proxy env, no fallback between deployments.
  - Don't claim compatibility with a server without a recorded live conformance run ([matrix](docs/architecture/model-compatibility.md)).
- Tools ([ADR-0014](docs/decisions/0014-tool-runtime-and-durable-lifecycle.md)–[0017](docs/decisions/0017-mcp-integration.md)):
  - Authorization lives in the fold: no `ToolExecutionStarted` without `allow` or a binding-matched approval; re-execution only for idempotent tools; otherwise reconcile or record `ambiguous`.
  - Policy reads only runtime-controlled facts. Never derive permissions from descriptions, MCP annotations or tool output.
  - Never label anything `Isolation.SANDBOX` without a verified backend; `process` is not a sandbox.
- Don't create packages or modules to reserve namespace. Create them when there is code for them.
- Don't add dependencies without a justification in the commit message. Architecture-shaping dependencies need an ADR.

## Testing expectations

- Every behavior change ships with a test. Test through public interfaces, and prefer shared fakes (fake provider, in-memory store) over mocks.
- The default suite never touches the network or needs an API key. Adapter tests use a real SDK client over a mock transport.
- Enforce important invariants with tests, not prose. Examples: import boundaries, illegal state transitions rejected.
- Run `make check` before declaring work done. Never report a check as passing without running it, and report actual results.

## Constraints

- Modular monolith. No microservices, distributed infrastructure, vector databases or model-training infrastructure.
- Local-first: everything must work on one machine. Storage sits behind a port. SQLite is the store, assuming a single process and a single active runner ([ADR-0004](docs/decisions/0004-local-first-sqlite-persistence.md)).
- Treat model output as untrusted input.
- Credentials are configured as env-var references, resolved only by the gateway via `hollis.adapters.credentials`, and go only to provider clients. Never put them in run specs, requests, descriptors, events or logs.
- Run input is persisted verbatim. Failure messages and logs are redacted, and registered secrets are refused in input. Arbitrary secrets in user text are **not** detected; don't claim otherwise ([requirements](docs/product/requirements.md#data-persistence-and-secret-handling), [ADR-0009](docs/decisions/0009-credentials-and-redaction.md)).

## Workflow

- Use Plan Mode for complex or high-impact changes. Approved, bounded tasks go straight to implementation.
- Work on a branch, in small commits that each pass `make check`, with conventional prefixes (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`, `ci:`).
- Keep this file short (target ≤ 100 lines). Detailed guidance belongs in `docs/` or in path-scoped rules or skills, added only when a need is proven ([docs/development.md](docs/development.md)).
