# Architecture Overview

Status: direction-setting. Implemented so far (M1–M2): the runtime core (`hollis.runtime`: events, state fold, RunStore and ModelProvider ports, failure taxonomy, budgets, redaction, single-turn runner) the model gateway (`hollis.gateway`: catalog, routing policy, resolution), and adapters (`hollis.adapters`: in-memory and SQLite stores, scripted fake provider, credential loader, endpoint config, Anthropic Messages and OpenAI-compatible Chat Completions protocol adapters). Each section separates **decided** from **open**. Decided items link to ADRs, and open items name the milestone that resolves them.

## Shape

Hollis is a **modular monolith**: one Python package (`hollis`) with internal modules that have enforced dependency directions ([ADR-0001](../decisions/0001-modular-monolith-python-uv.md)). The core depends only on interfaces ("ports"). Concrete providers and stores are adapters that depend on the core, never the other way round ([ADR-0002](../decisions/0002-ports-and-adapters.md)).

```
          SDK (public Python API)      CLI        REST API
                     \                  |            /
                      +---- interfaces depend on ---+
                                     |
                              runtime core
     (run state machine · event log · runner · budgets · policy)
                     |                |                 |
       ModelResolver/ModelProvider  RunStore        ToolExecutor      <- ports (in core)
                     |                |                 |
               model gateway          |                 |             <- hollis.gateway
   (catalog · routing policy · credentials)             |
                     |                |                 |
     protocol adapters (anthropic-    storage adapters  sandbox adapters  <- adapters (edge)
     messages, openai-chat-completions)
```

Packages appear only when there is code to put in them. Nothing gets created to reserve a name.

## Components

| Component | Responsibility | Milestone |
|---|---|---|
| **Runtime core** | Owns a run's lifecycle: state machine, runner, resume; later budgets and cancellation. The only writer of run events. | M1 (single turn) |
| **Event log / run record** | Append-only, ordered record of everything that happened in a run. The source of truth for inspection, resume and evaluation. | M1 |
| **Model provider port** | Provider-neutral request and response types per task family (text generation in M2), plus deployment descriptors and capabilities. | M1 (fake), M2 |
| **Model gateway** | Resolves logical model ids to deployments and protocol adapters; routing policy; credential resolution. No fallback or load balancing ([ADR-0011](../decisions/0011-model-gateway-and-deployments.md)). | M2 |
| **Storage port** | Persists run records and, later, memory. Adapters: in-memory, SQLite, later others. | M1 |
| **Tool system** | Typed tool schemas, argument validation, permission policy, sandboxed execution, result capture. | M3 |
| **Context manager** | Decides what goes into each model call within a token budget. | M4 |
| **Memory / retrieval** | Long-lived knowledge across runs, behind a storage port. | M4 |
| **Interfaces** | SDK first. CLI and REST API are thin layers over the same operations. | SDK M1+, CLI/API M5 |
| **Coordination** | Multi-agent composition built on runs, not a separate engine. | M6 |

## Runtime and harness

**Decided.** The runtime is the product's foundation. The core is async for I/O-bound paths: model calls, tool execution and storage ([ADR-0003](../decisions/0003-async-first-runtime.md)).

- **State from events.** Run state is a pure fold over an append-only, versioned event log, with an explicit transition table (NFR-1, [ADR-0005](../decisions/0005-event-recorded-run-state.md)).
- **States (M2):** `pending → model_call_in_flight → (model_responded → completed | attempt_failed → model_call_in_flight | failed)`. `pending` can also go to `failed` (deadline passed before the first attempt). Every attempt ends with a recorded outcome before another starts ([ADR-0007](../decisions/0007-failure-classification-retries-and-ambiguous-policy.md)).
- **No `interrupted` status.** A crashed run is simply a non-terminal run with no active runner.
- **No `cancelled` status** until cancellation exists.
- **One code path.** `Runner.execute` performs both first execution and recovery.
- **Model-call semantics** ([ADR-0006](../decisions/0006-model-call-execution-semantics.md), amended by [ADR-0007](../decisions/0007-failure-classification-retries-and-ambiguous-policy.md)). Hollis owns retries, and failures are classified as not processed or ambiguous. The ambiguous policy defaults to `fail` unless the deployment deduplicates. Budgets: attempts, output tokens, attempt timeout, deadline.
  - A stable operation id per logical call.
  - An intent is recorded before each attempt.
  - The provider is never reinvoked after a response is recorded.
  - Invocation is at-least-once on ambiguous outcomes. Exactly-once is **not** claimed.

**Open.**
- Multi-turn and tool-driven execution, and the events they need (M3).
- A limit on manual resumes of the same run (attempts are bounded; resumes are not).
- Idempotency strategy for non-repeatable tool side effects (M3).
- Concurrency model for parallel tool calls (M3).

## Model providers

**Decided.**
- The core defines its own request, response and usage types. No vendor SDK is imported outside an adapter (NFR-7). A scripted fake provider is a first-class adapter for tests (NFR-3).
- **Deployments separate identity, provider, protocol, endpoint and capabilities** ([ADR-0011](../decisions/0011-model-gateway-and-deployments.md)). The protocol selects the adapter; the provider label never does.
- **Protocol adapters** ([ADR-0012](../decisions/0012-protocol-adapters.md)): `anthropic-messages` (official SDK) and `openai-chat-completions` (raw `httpx2`). Each serves many servers, configured by data. No multi-provider library.
- **Capabilities are declared per deployment** and validated before persistence. Provider-specific options are typed per protocol, so nothing is silently dropped.
- **On-prem networking** ([ADR-0013](../decisions/0013-locality-routing-and-on-prem.md)): explicit endpoints, locality and routing policy, custom CA and mutual TLS, explicit proxies, no environment-driven routing, no SDK telemetry.
- Compatibility status per server: [model-compatibility.md](model-compatibility.md). Nothing has been verified live yet.

**Open.**
- Streaming, needed for responses above the non-streaming bound.
- Native adapters (OpenAI Responses API, Gemini, Bedrock, …) and further task families (embeddings, rerank, image, audio), each behind its own port.
- Typed per-run provider options. Options are currently per deployment.

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
- Secrets are referenced by name and resolved at execution time. They never enter persisted events, logs or model context unless explicitly allowed (NFR-4; target design, not yet met in M1 — see [the gap](../product/requirements.md#data-persistence-and-secret-handling)).
- Every tool call and its result is recorded.

**Open.**
- Sandbox technology per platform (M3).
- Network egress policy (M3).

## Persistence and deployment

**Decided.** Local-first (NFR-8). Storage sits behind a port.

**Decided.** SQLite is the initial durable store for run records ([ADR-0004](../decisions/0004-local-first-sqlite-persistence.md)):
- Stdlib `sqlite3`, one connection per operation in a worker thread.
- `BEGIN IMMEDIATE` appends, the default rollback journal, `PRAGMA user_version` migrations.
- Assumes a single process and a single active runner. Not encrypted at rest.
- Run input and instructions are stored verbatim. Failure messages and logs are redacted, and registered secrets are refused in input ([ADR-0009](../decisions/0009-credentials-and-redaction.md)). Retention is policy only, with no deletion mechanism ([ADR-0010](../decisions/0010-retention-and-deletion.md); [details](../product/requirements.md#data-persistence-and-secret-handling)).

**Open.**
- Cloud deployment target and packaging. Expected to be one container running the same monolith plus a networked database adapter, decided when a cloud use case is concrete.
- Leases or fencing for multiple executors (with cloud deployment).

## Evaluation and observability

**Direction.** The event log *is* the trajectory, so observability and evaluation build on the same record:
- Unit and integration tests verify runtime mechanics deterministically using the fake provider.
- Behavioral evals (M2+) assert specific actions in trajectories.
- End-to-end benchmarks come later as complementary evidence.

See [testing.md](testing.md).

**Open.**
- Tracing export format (for example, OpenTelemetry). SDK spans are disabled for now; any exporter must apply the `Redactor` to attributes.
- Cost accounting. Usage is recorded as reported; no reliable pricing source is integrated.

## Decided vs open

| Topic | Status | Reference |
|---|---|---|
| Modular monolith, Python 3.12+, uv | Accepted | ADR-0001 |
| Ports and adapters; vendor-neutral core | Accepted | ADR-0002 |
| Async-first for I/O-bound runtime paths | Accepted | ADR-0003 |
| SQLite as initial persistence (no WAL) | Accepted (M1) | ADR-0004 |
| Event-recorded run state, fold, optimistic concurrency | Accepted (M1) | ADR-0005 |
| Model-call semantics: operation ids, at-least-once, no exactly-once | Accepted (M1) | ADR-0006 |
| Pydantic v2 for validated domain and boundary types | Adopted (M1) | — |
| Failure taxonomy, Hollis-owned retries, ambiguous policy, budgets | Accepted (M2) | ADR-0007 |
| Anthropic adapter (optional extra, no SDK retries, no idempotency) | Accepted (M2) | ADR-0008 |
| Credential boundary and redaction | Accepted (M2) | ADR-0009 |
| Retention and deletion: whole-run, operator-configured | Policy accepted, mechanism open | ADR-0010 |
| Model gateway and deployment model | Accepted (M2) | ADR-0011 |
| Own protocol adapters; no multi-provider library | Accepted (M2) | ADR-0012 |
| Locality, routing policy, on-prem networking | Accepted (M2) | ADR-0013 |
| Streaming, native non-Anthropic adapters, more task families, cost accounting | Open | — |
| Sandbox technology, tool idempotency | Open | M3 |
| Retrieval backend | Open, chosen by benchmark | M4 |
| Cloud deployment target | Open | Undecided |
