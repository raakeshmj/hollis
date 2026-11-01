# Architecture Overview

Status: direction-setting. Implemented so far (M1–M7): multi-agent orchestration (`hollis.platform.orchestration` and the workflow coordinator, on lineage recorded by `hollis.runtime.delegation`), the product interfaces (`hollis.platform`: workspace, principal-aware services, auth, run supervision and views; `hollis.server`: the REST API; `hollis.sdk`: the embedded and remote SDK; `hollis.cli`; the web app in `web/`, served from `hollis.server`), the runtime core (`hollis.runtime`: events, state fold, RunStore, ModelProvider, embedding, tool, artifact, memory and knowledge ports, grounding checks, identifier-aware text analysis, failure taxonomy, budgets, redaction, the multi-turn tool loop, the context engine), the memory system (`hollis.memory`: records, write policy, lifecycle, recall, memory tools, episodes), the knowledge system (`hollis.knowledge`: extraction, chunking, ingestion lifecycle, hybrid retrieval, knowledge tools), the tool system (`hollis.tools`: `@tool` definitions, registry, rule-based policy, filesystem and command tools, MCP client), the model gateway (`hollis.gateway`: catalog, routing policy, resolution), and adapters (`hollis.adapters`: in-memory and SQLite stores, scripted fake provider, credential loader, endpoint config, Anthropic Messages and OpenAI-compatible Chat Completions protocol adapters). Each section separates **decided** from **open**. Decided items link to ADRs, and open items name the milestone that resolves them.

## Shape

Hollis is a **modular monolith**: one Python package (`hollis`) with internal modules that have enforced dependency directions ([ADR-0001](../decisions/0001-modular-monolith-python-uv.md)). The core depends only on interfaces ("ports"). Concrete providers and stores are adapters that depend on the core, never the other way round ([ADR-0002](../decisions/0002-ports-and-adapters.md)).

```
     web app (web/)          CLI (hollis.cli)
           |  HTTP                 |
      REST API (hollis.server)   SDK (hollis.sdk): embedded or remote ---- HTTP ----+
           \                       |                                                |
            +-------------- platform services (hollis.platform) <------------------+
                 workspace · principals and auth · run supervisor · views
                                     |
                              runtime core
     (run state machine · event log · runner · budgets · policy)
                     |                |                 |
       ModelResolver/ModelProvider  RunStore   ToolCatalog/Policy/Approver  <- ports (in core)
                     |                |                 |
               model gateway          |                 |             <- hollis.gateway
   (catalog · routing policy · credentials)             |
                     |                |                 |
     protocol adapters (anthropic-    storage adapters  hollis.tools: functions, fs,  <- edge
     messages, openai-chat-completions)                  command, MCP (no sandbox)
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
| **Tool system** | Typed tool definitions, argument validation, deterministic policy and approval, bounded execution with declared isolation, durable result capture, MCP ([ADR-0014](../decisions/0014-tool-runtime-and-durable-lifecycle.md)–[0017](../decisions/0017-mcp-integration.md)). No sandbox backend. | M3 |
| **Context engine** | Assembles each model call from instructions, input, memories, summaries, history and tools within a token budget; compacts long histories; records every plan ([ADR-0018](../decisions/0018-context-engine.md)). | M4 |
| **Artifact store** | Complete tool results outside the event log, referenced and read on demand ([ADR-0019](../decisions/0019-artifact-store.md)). | M4 |
| **Memory** | Governed, scoped, temporal agent memory across runs, behind a storage port ([ADR-0020](../decisions/0020-memory-model-and-lifecycle.md), [ADR-0021](../decisions/0021-memory-storage-retrieval-and-deletion.md)). | M4 |
| **Knowledge** | Ingestion with provenance and structure, hybrid retrieval with access control in the store, cited passages in context, knowledge tools, grounding checks ([ADR-0022](../decisions/0022-embedding-task.md)–[0025](../decisions/0025-rag-grounding-and-security.md)). | M5 |
| **Platform layer** | Workspace (`hollis.toml`, `.hollis/`), the one set of services every interface calls, with authorization by principal, background run supervision, the run index, views computed from the fold, the demo model ([ADR-0026](../decisions/0026-workspace-and-platform-layer.md), [ADR-0028](../decisions/0028-authentication-and-authorization.md)). | M6 |
| **Interfaces** | REST API (versioned, OpenAPI, problem details, SSE), the SDK (embedded or remote, async and sync), the CLI, the web app ([ADR-0027](../decisions/0027-rest-api-contract.md), [ADR-0029](../decisions/0029-web-application-and-design-system.md)). None re-derives run semantics. | M6 |
| **Orchestration** | Delegation as durable child runs: delegate tools, permission narrowing, limits and budgets, reports with verified evidence, plans on a deterministic coordinator, the orchestration view ([ADR-0030](../decisions/0030-orchestration-on-durable-runs.md)–[0032](../decisions/0032-a2a-interoperability.md)). Built on runs, not a separate engine. | M7 |

## Runtime and harness

**Decided.** The runtime is the product's foundation. The core is async for I/O-bound paths: model calls, tool execution and storage ([ADR-0003](../decisions/0003-async-first-runtime.md)).

- **State from events.** Run state is a pure fold over an append-only, versioned event log, with an explicit transition table (NFR-1, [ADR-0005](../decisions/0005-event-recorded-run-state.md)).
- **States (M3):** `pending → model_call_in_flight → (model_responded → completed | tool_calls → model_call_in_flight | attempt_failed → model_call_in_flight | failed)`. `pending` can also go to `failed` (deadline passed before the first attempt). Every attempt ends with a recorded outcome before another starts ([ADR-0007](../decisions/0007-failure-classification-retries-and-ambiguous-policy.md)). A run in `tool_calls` with unresolved approvals is suspended until `Runner.resolve_approval`.
- **Tool lifecycle** ([ADR-0014](../decisions/0014-tool-runtime-and-durable-lifecycle.md)): `ToolCallDecided → ToolApprovalResolved? → ToolExecutionStarted → ToolResultRecorded` per call. The fold enforces authorization, approval binding, batch holding, idempotent-only re-execution and limits, whichever code appends.
- **No `interrupted` status.** A crashed run is simply a non-terminal run with no active runner.
- **No `cancelled` status** until cancellation exists.
- **One code path.** `Runner.execute` performs both first execution and recovery.
- **Model-call semantics** ([ADR-0006](../decisions/0006-model-call-execution-semantics.md), amended by [ADR-0007](../decisions/0007-failure-classification-retries-and-ambiguous-policy.md)). Hollis owns retries, and failures are classified as not processed or ambiguous. The ambiguous policy defaults to `fail` unless the deployment deduplicates. Budgets: attempts, output tokens, attempt timeout, deadline.
  - A stable operation id per logical call.
  - An intent is recorded before each attempt.
  - The provider is never reinvoked after a response is recorded.
  - Invocation is at-least-once on ambiguous outcomes. Exactly-once is **not** claimed.

**Open.**
- A limit on manual resumes of the same run (attempts are bounded; resumes are not).
- Re-evaluating policy on resume. A recorded `allow` or approval currently stands after a policy change.
- Cancellation.

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

**Decided (M4).**
- **Assembly is structured and recorded.** Instructions, the verbatim input, context blocks (memories, the latest summary), the verbatim history window and tool specs stay separate. `ContextAssembled` records each call's plan, so retries send identical requests ([ADR-0018](../decisions/0018-context-engine.md)).
- **Budgets come from the deployment.** The declared context window minus the output reservation and a safety margin, or a configured cap. Estimates say how they were obtained: estimated, calibrated or provider-counted.
- **Levers under pressure only, least lossy first:** artifact references, compaction, dropping optional memories. Mandatory context that can't fit fails the run.
- **Compaction never alters the log.** The ledger is rebuilt from events; notes are immutable and carried forward unless resolved with a reason.
- **Artifacts:** complete tool results in a separate, run-scoped store ([ADR-0019](../decisions/0019-artifact-store.md)).
- **Memory:** typed records with slots, certainty capped by source, validity versus record time, and explicit update, correction, temporary and conflict semantics. Writes are governed; access is namespace-scoped per run and enforced in the store, manager, runner and fold. Memory never feeds authorization ([ADR-0020](../decisions/0020-memory-model-and-lifecycle.md)).
- **Storage and retrieval:** SQLite with FTS5; hybrid lexical, metadata, temporal and entity-link recall; no vector or graph database (chosen by [evaluation](evaluation.md)). Deletion is a hard delete with a tombstone, and run-log copies remain ([ADR-0021](../decisions/0021-memory-storage-retrieval-and-deletion.md)).

**Open.**
- Real embedding models: dense retrieval is wired through the gateway (M5) but evaluated only with local and simulated embedders.
- Model-assisted summarization and extraction quality with real models (no live deployment yet).
- Multi-process writers for the memory store.

## Tool execution and security

**Decided (M3).**
- Model output is untrusted input. Tool arguments are validated against the tool's schema before policy runs; invalid calls never execute.
- Policy is deterministic code over runtime-controlled facts (tool identity, declared effect and trust, environment, validated arguments). Descriptions, MCP annotations and tool output never grant permission. The default allows only trusted read-only tools; everything else needs approval, and without an approver it is denied ([ADR-0015](../decisions/0015-tool-definitions-policy-and-approval.md)).
- Approvals are bound to the exact run, invocation, tool version and arguments, and live only in events.
- Side effects are never blindly repeated: re-execution only for tools declared idempotent; otherwise reconcile or record `ambiguous`. No exactly-once claim.
- Registered secrets are redacted from tool arguments, continuations and results; calls carrying them are denied. External results are framed as untrusted data.
- Isolation is declared honestly per tool: `none` (in-process) or `process` (separate process, same privileges). **Nothing is sandboxed**; `sandbox` is reserved for a verified backend ([ADR-0016](../decisions/0016-execution-isolation.md)).
- MCP tools are opt-in per tool, external, approval-required by default, and pinned or locked out on definition change ([ADR-0017](../decisions/0017-mcp-integration.md)).

**Open.**
- A sandbox backend with verified isolation per platform.
- Network egress control for tools.
- An artifact store for tool output above the size limit.
- Approver identity and an approval UI.

## Knowledge retrieval

**Decided (M5).**
- **Knowledge is separate from memory:** own store, lifecycle, access model and context kind. It never writes memory; memory is never presented as a source.
- **Embeddings are a gateway task** with spaces that are never mixed. There's an OpenAI-compatible adapter and a local model-free hashing embedder ([ADR-0022](../decisions/0022-embedding-task.md)).
- **Ingestion:**
  - (collection, key) identity; content and index fingerprints; atomic version swaps; directory sync with deletion.
  - Content-sniffed extraction for Markdown, HTML, code, JSON, CSV, DOCX and PDF; structure-aware chunks with locations.
  - Identifier-aware analysis ([ADR-0023](../decisions/0023-knowledge-ingestion.md)).
- **Retrieval:**
  - SQLite FTS5, an exact identifier index and per-space vectors (exact scan).
  - Access enforced inside every query.
  - RRF fusion with exact-first and a follow-up hop, all chosen by [evaluation](rag-evaluation.md) ([ADR-0024](../decisions/0024-knowledge-storage-and-retrieval.md)).
- **In runs:**
  - Passages are retrieved once at run start, recorded, and cited by key. They're dropped before memories under pressure.
  - Knowledge tools support iterative investigation.
  - A deterministic grounding report covers each final answer ([ADR-0025](../decisions/0025-rag-grounding-and-security.md)).

**Open.**
- Real embedding model quality and a public retrieval benchmark.
- An approximate vector index beyond about 10,000 chunks per query scope.
- Cross-encoder reranking as a gateway task.
- Web and enterprise connectors; OCR and multimodal retrieval.

## Product interfaces

**Decided (M6).**
- One application layer, `hollis.platform`: every interface calls `Platform` services with the calling principal; views are computed from the runtime's fold and events ([ADR-0026](../decisions/0026-workspace-and-platform-layer.md)).
- Runs execute in the background in the process that owns the workspace, independent of connections; shutdown leaves them resumable and server start resumes interrupted ones. One process owns the stores: CLI commands use a running server automatically.
- REST under `/v1` with RFC 9457 errors, SSE with `Last-Event-ID` and long polling; the SDK's two backends return the same models and errors ([ADR-0027](../decisions/0027-rest-api-contract.md)).
- Authentication always required; loopback by default; hashed tokens, single-use sign-in links, strict cookies with an Origin check, a Host allowlist; authorization by role, grants intersected with agents, run visibility, approval identity ([ADR-0028](../decisions/0028-authentication-and-authorization.md)).
- A web app on the same origin, built into the package, with a design system from the brand ([ADR-0029](../decisions/0029-web-application-and-design-system.md), [brand guide](../../brand/README.md)).

**Open.** TLS termination and external identity providers (behind a proxy today); more than one process per workspace; run deletion.

## Orchestration

**Decided (M7).**
- **A delegation is a tool call whose execution is a child run** ([ADR-0030](../decisions/0030-orchestration-on-durable-runs.md)):
  - Children are ordinary runs: same runner, events, approvals, cancellation and views.
  - Policy, approval, read-only parallelism and recovery come from the tool lifecycle.
  - Child ids derive from (parent run, invocation), so a re-executed delegation attaches to its child: no duplicates.
- **Lineage, the structured assignment and delegation limits are recorded** in the child's `RunCreated`. The run index derives the tree from the logs.
- **Collaboration is structured:**
  - assignments down, with earlier results passed by reference along with their verified evidence;
  - reports up, built from the child's log: outcome, usage, unknown side effects, citation check, evidence with stable handles, a validated self-report, and the answer framed as untrusted;
  - disagreements kept, not resolved.
- **Plans** run on the deterministic workflow coordinator: waves of ready steps, skipped dependents, summary or hand-off.
- **Authority only narrows** ([ADR-0031](../decisions/0031-orchestration-authority-and-limits.md)):
  - A child's access is its agent's configuration within the parent run's access within the owner's current grants.
  - Delegate tools declare the strongest effect they can reach.
  - Depth, children, runs, a token budget with a coordinator reserve (enforced by a runner guard) and wait limits bound every tree.
  - Cancellation cascades.
- **A2A** is mapped at the boundary but not implemented, and internal orchestration does not depend on it ([ADR-0032](../decisions/0032-a2a-interoperability.md)).

**Open.**
- Live models as coordinators and specialists (opt-in evaluation written, not run).
- Suspending a parent instead of waiting in the tool, if human-in-the-loop children routinely exceed an hour.
- A2A server and client.
- Cost-based budgets.

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
- Context and memory are evaluated offline against baselines ([evaluation.md](evaluation.md)); behavioral evals against real models await live access.
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
| Durable tool lifecycle, recovery, results and framing | Accepted (M3) | ADR-0014 |
| Tool definitions, registry, policy and approval | Accepted (M3) | ADR-0015 |
| Execution isolation declared per tool; no sandbox claimed | Accepted (M3) | ADR-0016 |
| MCP via the official SDK; allowlisted, pinned, external | Accepted (M3) | ADR-0017 |
| Streaming, native non-Anthropic adapters, more task families, cost accounting | Open | — |
| Sandbox backend, tool egress control | Open | Execution backends |
| Context engine: recorded plans, budgets, levers, compaction | Accepted (M4) | ADR-0018 |
| Artifact store for complete tool results | Accepted (M4) | ADR-0019 |
| Memory model, lifecycle and scope | Accepted (M4) | ADR-0020 |
| Memory storage (SQLite FTS5), retrieval and deletion semantics | Accepted (M4) | ADR-0021 |
| Embeddings as a gateway task; embedding spaces | Accepted (M5) | ADR-0022 |
| Knowledge ingestion, structure, identifier-aware analysis | Accepted (M5) | ADR-0023 |
| Knowledge storage, hybrid retrieval, access control in the store | Accepted (M5) | ADR-0024 |
| RAG grounding, citations, trust, deletion | Accepted (M5) | ADR-0025 |
| Approximate vector index, cross-encoder reranking, connectors, multimodal | Open | — |
| Workspace and one platform layer for every interface | Accepted (M6) | ADR-0026 |
| REST contract, long-running work (SSE, long polling), SDK backends | Accepted (M6) | ADR-0027 |
| Authentication and authorization | Accepted (M6) | ADR-0028 |
| Web application and design system | Accepted (M6) | ADR-0029 |
| Orchestration as delegation to durable child runs; plans on a deterministic coordinator | Accepted (M7) | ADR-0030 |
| Authority, limits and budgets for delegated runs | Accepted (M7) | ADR-0031 |
| A2A: mapped boundary, implementation deferred | Accepted (M7) | ADR-0032 |
| TLS, external identity providers, multi-process workspaces | Open | — |
| Cloud deployment target | Open | Undecided |
