# Architecture Overview

Status: direction-setting. Nothing below is implemented yet except the CLI stub. Each section separates **decided** from **open**. Decided items link to ADRs, and open items name the milestone that resolves them.

## Shape

Hollis is a **modular monolith**: one Python package (`hollis`) with internal modules that have enforced dependency directions ([ADR-0001](../decisions/0001-modular-monolith-python-uv.md)). The core depends only on interfaces ("ports"). Concrete providers and stores are adapters that depend on the core, never the other way round ([ADR-0002](../decisions/0002-ports-and-adapters.md)).

```
          SDK (public Python API)      CLI        REST API
                     \                  |            /
                      +---- interfaces depend on ---+
                                     |
                              runtime core
     (run state machine · event log · step loop · budgets · policy)
                     |                |                 |
              ModelProvider       RunStore          ToolExecutor      <- ports (in core)
                 port               port               port
                     |                |                 |
             provider adapters   storage adapters   sandbox adapters  <- adapters (edge)
```

Packages appear only when there is code to put in them. Nothing gets created to reserve a name.

## Components

| Component | Responsibility | Milestone |
|---|---|---|
| **Runtime core** | Owns a run's lifecycle: state machine, step loop, budget enforcement, cancellation, resume. The only writer of run events. | M1 |
| **Event log / run record** | Append-only, ordered record of everything that happened in a run. The source of truth for inspection, resume and evaluation. | M1 |
| **Model provider port** | Provider-neutral request and response types (messages, tool calls, usage). Adapters translate to vendor APIs. | M1 (fake), M2 |
| **Storage port** | Persists run records and, later, memory. Adapters: in-memory, SQLite, later others. | M1 |
| **Tool system** | Typed tool schemas, argument validation, permission policy, sandboxed execution, result capture. | M3 |
| **Context manager** | Decides what goes into each model call within a token budget. | M4 |
| **Memory / retrieval** | Long-lived knowledge across runs, behind a storage port. | M4 |
| **Interfaces** | SDK first. CLI and REST API are thin layers over the same operations. | SDK M1+, CLI/API M5 |
| **Coordination** | Multi-agent composition built on runs, not a separate engine. | M6 |

## Runtime and harness

**Decided.** The runtime is the product's foundation. A run is an explicit state machine (for example, `pending → running → {completed, failed, cancelled}`, with a resumable `interrupted` state). Transitions are validated in code (NFR-1). The core is async for I/O-bound paths: model calls, tool execution and storage ([ADR-0003](../decisions/0003-async-first-runtime.md)).

**Proposed.** Run state is derived from an append-only event log, and resume works by replaying it ([ADR-0005](../decisions/0005-event-recorded-run-state.md)). Side-effecting steps record their intent before execution and their result after, so resume can tell which steps finished.

**Open.**
- Exact state set and event schema (M1).
- Idempotency strategy for non-repeatable tool side effects (M3).
- Concurrency model for parallel tool calls (M3).

## Model providers

**Decided.** The core defines its own message, tool-call and usage types. No vendor SDK is imported outside an adapter (NFR-7, enforced by tests from M1). A deterministic scripted fake provider is a first-class adapter for testing (NFR-3).

**Open.**
- Which real provider comes first (M2).
- Streaming interface shape (M2).
- Normalizing provider-specific features (structured output, caching, reasoning) without lowest-common-denominator loss (M2+).

## Context and memory

**Direction.** Context assembly is an explicit, inspectable step. Each model call records what was included and why. Memory is a storage concern behind a port, not a special subsystem.

**Open.**
- Summarization and truncation strategies (M4).
- Retrieval backend: chosen by benchmark in M4. No vector database before then (see non-goals).

## Tool execution and security

**Principles (decided).**
- Model output is untrusted input. Tool arguments are validated against typed schemas before anything runs.
- Tools run with least privilege under an explicit policy (allow, deny or ask) attached to the agent or run.
- Execution goes through a `ToolExecutor` port, so isolation (subprocess, container, remote) is swappable.
- Secrets are referenced by name and resolved at execution time. They never enter persisted events, logs or model context unless explicitly allowed (NFR-4).
- Every tool call and its result is recorded.

**Open.**
- Sandbox technology per platform (M3).
- Network egress policy (M3).

## Persistence and deployment

**Decided.** Local-first (NFR-8). Storage sits behind a port.

**Proposed.** SQLite is the initial durable store for run records ([ADR-0004](../decisions/0004-local-first-sqlite-persistence.md)).

**Open.**
- Cloud deployment target and packaging. Expected to be one container running the same monolith plus a networked database adapter, decided when a cloud use case is concrete.
- Schema migration tooling (M1, if SQLite is accepted).

## Evaluation and observability

**Direction.** The event log *is* the trajectory, so observability and evaluation build on the same record:
- Unit and integration tests verify runtime mechanics deterministically using the fake provider.
- Behavioral evals (M2+) assert specific actions in trajectories.
- End-to-end benchmarks come later as complementary evidence.

See [testing.md](testing.md).

**Open.**
- Tracing export format (for example, OpenTelemetry), M2+.
- Cost accounting granularity (M2).

## Decided vs open

| Topic | Status | Reference |
|---|---|---|
| Modular monolith, Python 3.12+, uv | Accepted | ADR-0001 |
| Ports and adapters; vendor-neutral core | Accepted | ADR-0002 |
| Async-first for I/O-bound runtime paths | Accepted | ADR-0003 |
| SQLite as initial persistence | Proposed, decided in M1 | ADR-0004 |
| Event-recorded run state and replay-based resume | Proposed, validated in M1 | ADR-0005 |
| Pydantic v2 for validated domain and boundary types | Product constraint, adopted in M1 when first used | — |
| First real provider, streaming shape | Open | M2 |
| Sandbox technology, tool idempotency | Open | M3 |
| Retrieval backend | Open, chosen by benchmark | M4 |
| Cloud deployment target | Open | Undecided |
