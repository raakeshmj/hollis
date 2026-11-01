# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

Hollis is a model-agnostic, local-first platform for building, running and orchestrating autonomous agents: a Python SDK, a CLI, a REST API and a web app over one platform layer. The **runtime and harness** are the foundation. Coding agents are one specialization, so don't let the core assume coding tasks.

**Current milestone: M7 (multi-agent orchestration) is implemented and verified offline, on M2–M6. No live model, embedding model or third-party MCP server has been tested, and no sandbox exists. No later milestone is planned yet.** Don't implement unplanned work. See [docs/product/roadmap.md](docs/product/roadmap.md).

## Repository map

- `src/hollis/runtime/`: the core. Events and schema registry, state fold, RunStore and ModelProvider ports, the tool port (`tools.py`), artifact and memory ports, delegation facts (`delegation.py`: lineage, assignments, limits, child ids), the context engine (`context.py` types, `assembly.py` planning, `compaction.py` summarizers), conversation history, result handling, failure taxonomy, budgets, `Redactor`, `Runner`. It imports only stdlib, pydantic and itself (enforced by `tests/architecture/`).
- `src/hollis/gateway/`: deployment catalog (Python or TOML), presets, routing policy, `ModelGateway` (implements the runtime's `ModelResolver`).
- `src/hollis/tools/`: `@tool` function tools, `ToolRegistry`, `RulePolicy`, built-in filesystem and command tools (`builtin/`), MCP client (`mcp.py`, optional extra).
- `src/hollis/knowledge/`: extraction (`extract.py`), sanitization, chunking, ingestion lifecycle (`ingest.py`, `KnowledgeBase`), hybrid retrieval (`retrieve.py`), knowledge tools. Separate from memory. SQLite lives in `adapters/sqlite_knowledge.py`; embeddings in `adapters/openai_embeddings.py` and `adapters/local_embeddings.py`.
- `src/hollis/memory/`: memory records, write policy, `MemoryManager` (lifecycle, recall, deletion, maintenance), memory tools, episodes and extraction. Storage-independent; SQLite lives in `adapters/sqlite_memory.py`.
- `src/hollis/adapters/`: stores, fake provider, the deterministic demo model (`demo_model.py`), credentials, endpoint config (`config.py`, SDK-free), shared HTTP layer (`_http.py`), protocol adapters `anthropic_messages.py` (official SDK) and `openai_chat.py` (raw httpx2). Provider SDKs and httpx2 are optional extras, loaded only by their adapter (enforced by subprocess tests).
- `src/hollis/platform/`: the one application layer ([ADR-0026](docs/decisions/0026-workspace-and-platform-layer.md)): workspace (`hollis.toml` + `.hollis/`), `Platform` services (every method takes the calling principal), auth, run supervisor, views computed from the fold, API schemas; orchestration (`orchestration.py`: delegate tools, narrowing, limits, budget guard, reports, tree view) and the deterministic plan coordinator (`coordinator.py`).
- `src/hollis/server/` (FastAPI, extra `server`; `static/` is the committed web build), `src/hollis/sdk/` (embedded `LocalBackend`, HTTP `RemoteBackend`, async and sync clients), `src/hollis/cli/` (Click on the SDK).
- `web/`: the web app (React, TypeScript, Vite); `brand/`: identity assets and guide; `examples/`: runnable SDK and CLI examples.
- `tests/`: `platform/`, `server/`, `sdk/`, `cli/`, `ui/` (web build; Chrome journeys opt-in), `runtime/`, `tools/`, `memory/`, `knowledge/`, `evals/` (memory and compaction: `python -m tests.evals.report`; retrieval: `python -m tests.evals.rag.report`; orchestration: `python -m tests.evals.orchestration.report`), `security/` (agentic attack scenarios), `adapters/` (mock transports), `gateway/`, `network/` (real loopback sockets and TLS), `conformance/` (offline, plus opt-in live via `HOLLIS_CONFORMANCE_*`), `recovery/`, `architecture/`, `live/`. Shared helpers live in `tests/support/`.
- `docs/product/`: vision, requirements (FR-x / NFR-x IDs), roadmap. `docs/architecture/`: `overview.md` (components, decided vs open), `testing.md`, `evaluation.md`, `rag-evaluation.md`, `orchestration-evaluation.md`.
- `docs/decisions/`: ADRs (template `0000-template.md`). `docs/plans/`: milestone plans (`m7-orchestration.md` is the most recent).
- `docs/guides/` and `docs/reference/`: installation, quickstart, SDK, web, orchestration, deployment; CLI, REST API, configuration.
- `docs/research/`: research notes (incl. multi-agent). `docs/development.md`: workflow, ADR policy, web workflow, when to add skills, rules, hooks or subagents.

**Before making an architectural decision**, read `docs/architecture/overview.md` and the relevant ADRs. If the change is load-bearing and costly to reverse, propose a new ADR rather than deciding silently.

## Commands

```bash
make sync                    # uv sync --locked (dev includes all provider extras)
make check                   # ruff format --check, ruff check, pyright (strict), pytest; CI runs this (make fix applies fixes)
uv run pytest tests/test_cli.py::test_version_flag_prints_version_and_exits_zero   # single test
uv add <pkg> / uv add --dev <pkg>   # updates uv.lock (commit it)
make web-check / web-build / web-api / web-e2e   # web app (Node 22.12+); see docs/development.md
HOLLIS_LIVE_TESTS=1 ANTHROPIC_API_KEY=... uv run pytest tests/live -m live   # opt-in, costs money
```

## Conventions

- Python ≥ 3.12, fully annotated; pyright strict reports 0 errors. Ruff (line length 100) is configured in `pyproject.toml`; no `noqa` or `type: ignore` without a reason on the same line.
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
- Context and memory ([ADR-0018](docs/decisions/0018-context-engine.md)–[0021](docs/decisions/0021-memory-storage-retrieval-and-deletion.md)):
  - A request is a pure function of state and its recorded `ContextAssembled` plan. Never send history the plan doesn't describe, and never summarize the run input.
  - Summaries are derived: never alter or drop events; notes are only added or resolved with a reason.
  - Memory writes go through `MemoryManager` (policy, slots); memory access only through the run's `MemoryAccess` grant. Memory never feeds tool policy.
  - Deleting a memory doesn't erase run-log copies; don't claim otherwise.
- Knowledge ([ADR-0022](docs/decisions/0022-embedding-task.md)–[0025](docs/decisions/0025-rag-grounding-and-security.md)):
  - Access is enforced in the store's SQL before scoring, and re-checked by the runner and fold; document content never sets labels, trust or identity.
  - Never compare vectors across embedding spaces; changing models means `migrate_embeddings`.
  - Knowledge never writes memory. Retrieved text is data: framed, citation keys assigned by Hollis, never an input to authorization.
  - Dense results from the hashing or simulated embedders are not semantic-quality evidence; don't claim otherwise.
- Orchestration ([ADR-0030](docs/decisions/0030-orchestration-on-durable-runs.md)–[0032](docs/decisions/0032-a2a-interoperability.md)): a delegation is a tool call executed as a child run with a deterministic id (no second engine, no way to create a child twice). Child access only narrows (agent ∩ parent run ∩ owner's current grants); reports come from the child's log, and child text never supplies evidence. Every tree is bounded; single agent stays the default.
- Interfaces ([ADR-0026](docs/decisions/0026-workspace-and-platform-layer.md)–[0029](docs/decisions/0029-web-application-and-design-system.md)):
  - Behaviour and authorization live in `Platform` services; the API, SDK, CLI and web app only call them and render views. Never re-derive run semantics in an interface.
  - Authentication is never optional; bind to loopback by default; resources a caller can't see answer 404.
  - Every API change: run `make web-api` and rebuild the web app (a test checks the contract). Label the demo model wherever it appears; add no setting without a backend capability.
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
- Work on a branch, in small commits that each pass `make check` (check its exit status, not piped output), with conventional prefixes (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`, `ci:`).
- Keep this file short (target ≤ 100 lines). Detailed guidance belongs in `docs/` or in path-scoped rules or skills, added only when a need is proven ([docs/development.md](docs/development.md)).
