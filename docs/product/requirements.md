# Requirements

IDs are stable. Cite them from ADRs, plans and tests (for example, "covers NFR-2"). The milestone column is the planned *first* delivery, not final completeness. See [roadmap.md](roadmap.md).

## Functional

| ID | Requirement | Milestone |
|---|---|---|
| FR-1 | Define an agent in Python: instructions, model selection, tools and limits | M1–M3 (M3: `RunSpec.tools` plus a tool catalog) |
| FR-2 | Execute a run to completion, failure or cancellation, with an explicit state at every point | M1 (completion, failure), M3 (multi-turn tool loop, suspension awaiting approval); cancellation later |
| FR-3 | Persist every run as an ordered record of events (model calls, tool calls, results, state transitions) | M1 |
| FR-4 | Resume an interrupted run from its persisted record. Never re-invoke a side effect whose result is durably recorded. Retry unrecorded outcomes under the same operation id, with no exactly-once claim ([ADR-0006](../decisions/0006-model-call-execution-semantics.md)). | M1 |
| FR-5 | Call models through a provider-neutral interface, so swapping providers needs no agent code changes | M1 (fake), M2 (gateway: Anthropic Messages and OpenAI-compatible protocols) |
| FR-14 | Configure model deployments independently of providers: protocol, endpoint (cloud, private, enterprise, localhost, on-prem), wire model id, auth by reference, TLS and proxy, declared capabilities | M2 ([ADR-0011](../decisions/0011-model-gateway-and-deployments.md)) |
| FR-6 | Define tools with typed, validated inputs and outputs | **M3 (offline):** `@tool` derives one schema for model and validation; MCP tools validate against the server schema ([ADR-0015](../decisions/0015-tool-definitions-policy-and-approval.md)) |
| FR-7 | Execute tools under an explicit permission and sandbox policy | **M3, partially:** deterministic policy and approval are enforced in the event fold ([ADR-0014](../decisions/0014-tool-runtime-and-durable-lifecycle.md)); isolation is declared per tool, but **no sandbox backend exists** ([ADR-0016](../decisions/0016-execution-isolation.md)) |
| FR-8 | Manage context: select, truncate and summarize what is sent to the model within a budget | **M4 (offline):** context engine with recorded plans, token budgets, compaction and artifact references ([ADR-0018](../decisions/0018-context-engine.md), [ADR-0019](../decisions/0019-artifact-store.md)) |
| FR-9 | Persistent memory and knowledge retrieval behind a storage adapter | **M4: agent memory** (governed writes, slots, temporal validity, hybrid lexical recall, scopes, deletion; [ADR-0020](../decisions/0020-memory-model-and-lifecycle.md), [ADR-0021](../decisions/0021-memory-storage-retrieval-and-deletion.md)). **M5: knowledge retrieval** (ingestion with provenance, hybrid retrieval, access control, citations; [ADR-0023](../decisions/0023-knowledge-ingestion.md)–[ADR-0025](../decisions/0025-rag-grounding-and-security.md)) |
| FR-10 | CLI to start, list, inspect and resume runs | M6 |
| FR-11 | REST API exposing the same operations as the SDK | M6 |
| FR-12 | Coordinate several agents on one task, with scoped tools and context per agent | M7 |
| FR-15 | Ingest, update and delete external knowledge sources with provenance; retrieve grounded, cited evidence under access control | **M5 (offline):** see [rag-evaluation](../architecture/rag-evaluation.md); live semantic quality unverified |
| FR-16 | Embedding models as a gateway task, local or remote | **M5 (offline)** ([ADR-0022](../decisions/0022-embedding-task.md)) |
| FR-13 | Export run trajectories in a form that behavioral evaluations can assert against | M1 (format), M2+ (eval tooling) |

## Non-functional

| ID | Requirement |
|---|---|
| NFR-1 **Correctness** | State transitions are validated. Illegal transitions are impossible through the public API. |
| NFR-2 **Recoverability** | A process crash at any point leaves a run resumable or explicitly failed, never silently corrupted. |
| NFR-3 **Determinism in tests** | The runtime can be driven by a scripted fake provider, so runs are reproducible without network access. |
| NFR-4 **Security** | Tools run with least privilege. Secrets never appear in persisted events or logs. Model output is treated as untrusted input. **Partially met (M2):** credentials and registered secrets never reach events or logs; arbitrary secrets inside user-supplied text are not detected. See [below](#data-persistence-and-secret-handling). **M3:** tools are least-privilege by default policy (only trusted read-only tools run without approval); registered secrets are redacted from tool arguments and results; external tool output is framed as untrusted data. Tool execution is **not sandboxed**. **M4:** registered secrets and credential patterns are refused in memory writes and redacted from summaries and artifacts; recalled memories and summaries are framed as data and never feed authorization; memory access is scoped per run. |
| NFR-5 **Explicit state** | No hidden global state. Everything that affects a run is in its configuration or event record. |
| NFR-6 **Resource limits** | Every run has enforceable budgets (steps, tokens, wall-clock time, cost) and stops cleanly when one is exceeded. **M2:** attempts, output tokens, per-attempt timeout and run deadline are enforced; **M3:** model turns, tool calls per run and per turn, tool parallelism, tool timeouts and tool output size; **M4:** input-token budget per call from the declared context window or a configured cap (estimated, calibrated or provider-counted) ([ADR-0007](../decisions/0007-failure-classification-retries-and-ambiguous-policy.md)); cost and input-token budgets are not (no reliable pricing source, no pre-estimation). |
| NFR-7 **Vendor neutrality** | The core imports no model-vendor or storage-vendor SDK. Adapters do. Enforced by tests from M1. |
| NFR-8 **Local-first** | The full feature set works on one machine with no external services other than the chosen model provider. |
| NFR-9 **Observability** | Every run can be inspected after the fact: what was sent, what came back, what ran, how long it took and what it cost. **M4:** each model call records how its context was assembled (history start, cleared results, memories, estimate, budget, exclusions), and recalled memories and summaries are events. **M5:** retrieved passages (ids, versions, scores) and a grounding report per final answer are recorded. Cost is still not computed. |
| NFR-10 **Typed API** | The public Python API is fully typed and passes pyright strict. |
| NFR-11 **Controlled egress** | Run data goes only to the resolved deployment's configured endpoint. No silent fallback, no environment-driven routing, no telemetry to third parties, and a routing policy can confine runs to local/private deployments ([ADR-0013](../decisions/0013-locality-routing-and-on-prem.md)). **M2:** implemented and tested offline. |

## Data persistence and secret handling

This section is the canonical statement of what Hollis persists and what it guarantees about secrets. Other documents link here.

**What is persisted** (event payloads, in memory or in SQLite):
- Run `input` and `instructions`, **verbatim**. They are never rewritten, because recovery must resend the identical request.
- Logical model id, the deployment reference (provider label, protocol, wire model, endpoint origin, locality, idempotency), budget and ambiguous-outcome policy. Credentials are never included.
- Model response content (registered secret values removed), stop reason, usage, served model, provider request id.
- Model tool calls and provider continuations (registered secret values removed), tool decisions with validated arguments, approvals (approver label and reason), and tool results after redaction and truncation (M3).
- Per-attempt failures (`ModelCallFailed`) and run failures (`RunFailed`): kind, provider request id, and a sanitized, redacted message.
- Context plans, compaction summaries (registered secrets removed) and recalled memories as shown to the model (M4).
- Retrieved knowledge passages as shown to the model, and grounding reports (M5).

**Outside the event log (M4):**
- **Artifact store** (optional, own database): complete tool results above the artifact threshold, after redaction of registered secrets.
- **Memory store** (optional, own database): memory records, an append-only change log, and job markers. Writes refuse registered secrets and credential-shaped text.
- **Knowledge store** (optional, own database): extracted chunks, sections, identifier and full-text indexes, embedding vectors, source metadata, and a content-free change log. Registered secrets are redacted before indexing. Embeddings are treated as sensitive as their text: same access control, deleted together.

**Status of the M2 requirements** (set at the end of M1):

| # | Requirement | Status |
|---|---|---|
| 1 | Credential references: raw credentials never in run configuration, `ModelRequest` or events | **Met (M2).** Keys load through `hollis.adapters.credentials` as `SecretStr` and go only to the provider client. Deployments hold env-var references, resolved by the gateway; runs record the deployment reference ([ADR-0009](../decisions/0009-credentials-and-redaction.md)). |
| 2 | Redaction before persistence and logging | **Met for diagnostics and registered values.** Failure messages and runtime logs: registered values plus credential patterns. Response content: registered values. Run input is not redacted; input containing a registered secret is refused. |
| 3 | Sanitized provider errors | **Met (M2).** The Anthropic adapter builds messages from status, error type and a truncated API message, with no bodies or headers; the runtime redacts them ([ADR-0008](../decisions/0008-anthropic-adapter.md)). |
| 4 | Tests proving secrets don't reach events or logs | **Met (M2)** for registered secrets: planted-key tests check serialized events, SQLite file bytes and captured logs. |
| 5 | Retention and deletion policy | **Runs: policy only.** Whole-run deletion with operator-configured retention ([ADR-0010](../decisions/0010-retention-and-deletion.md)); the mechanism isn't implemented, and the run store still refuses all deletes. **Memories (M4): implemented.** Hard delete with a content-free tombstone, `secure_delete`, retention rules ([ADR-0021](../decisions/0021-memory-storage-retrieval-and-deletion.md)). **Artifacts:** `delete_run` per run. **Knowledge (M5):** source and collection deletion remove chunks, index entries and vectors; original files and run-log copies remain ([ADR-0025](../decisions/0025-rag-grounding-and-security.md)). |

**What is still not guaranteed:**
- **Arbitrary secrets in user-supplied text are not detected.** Only registered values are refused in input. Pattern-shaped or unknown-shape secrets in input are persisted verbatim.
- `extra="forbid"` rejects undeclared fields only. It doesn't inspect string values.
- Model responses can contain sensitive data that isn't a registered secret. It's stored as returned.
- The same applies to tool arguments and tool results: only registered secrets are removed. A tool that returns unregistered sensitive data has it persisted.
- SQLite files are created with mode `0600` but are **not encrypted at rest**.
- No deletion or retention mechanism exists for runs yet (requirement 5).
- **Deleting a memory doesn't erase copies of it:** run logs that recalled it, responses or tool results that repeated it, the source events it came from, artifacts, or backups. It removes the memory record and its index entry.
- Memory writes can't detect arbitrary sensitive information (for example, personal data that isn't a credential). Episodes and extraction copy run content into memory when the application calls them.
