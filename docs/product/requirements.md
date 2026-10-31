# Requirements

IDs are stable. Cite them from ADRs, plans and tests (for example, "covers NFR-2"). The milestone column is the planned *first* delivery, not final completeness. See [roadmap.md](roadmap.md).

## Functional

| ID | Requirement | Milestone |
|---|---|---|
| FR-1 | Define an agent in Python: instructions, model selection, tools and limits | M1–M3 |
| FR-2 | Execute a run to completion, failure or cancellation, with an explicit state at every point | M1 (completion, failure); cancellation later |
| FR-3 | Persist every run as an ordered record of events (model calls, tool calls, results, state transitions) | M1 |
| FR-4 | Resume an interrupted run from its persisted record. Never re-invoke a side effect whose result is durably recorded. Retry unrecorded outcomes under the same operation id, with no exactly-once claim ([ADR-0006](../decisions/0006-model-call-execution-semantics.md)). | M1 |
| FR-5 | Call models through a provider-neutral interface, so swapping providers needs no agent code changes | M1 (fake), M2 (real) |
| FR-6 | Define tools with typed, validated inputs and outputs | M3 |
| FR-7 | Execute tools under an explicit permission and sandbox policy | M3 |
| FR-8 | Manage context: select, truncate and summarize what is sent to the model within a budget | M4 |
| FR-9 | Persistent memory and knowledge retrieval behind a storage adapter | M4 |
| FR-10 | CLI to start, list, inspect and resume runs | M5 |
| FR-11 | REST API exposing the same operations as the SDK | M5 |
| FR-12 | Coordinate several agents on one task, with scoped tools and context per agent | M6 |
| FR-13 | Export run trajectories in a form that behavioral evaluations can assert against | M1 (format), M2+ (eval tooling) |

## Non-functional

| ID | Requirement |
|---|---|
| NFR-1 **Correctness** | State transitions are validated. Illegal transitions are impossible through the public API. |
| NFR-2 **Recoverability** | A process crash at any point leaves a run resumable or explicitly failed, never silently corrupted. |
| NFR-3 **Determinism in tests** | The runtime can be driven by a scripted fake provider, so runs are reproducible without network access. |
| NFR-4 **Security** | Tools run with least privilege. Secrets never appear in persisted events or logs. Model output is treated as untrusted input. **Not met by M1** for secrets contained in user-supplied strings. See [below](#data-persisted-in-m1-and-secret-handling). |
| NFR-5 **Explicit state** | No hidden global state. Everything that affects a run is in its configuration or event record. |
| NFR-6 **Resource limits** | Every run has enforceable budgets (steps, tokens, wall-clock time, cost) and stops cleanly when one is exceeded. |
| NFR-7 **Vendor neutrality** | The core imports no model-vendor or storage-vendor SDK. Adapters do. Enforced by tests from M1. |
| NFR-8 **Local-first** | The full feature set works on one machine with no external services other than the chosen model provider. |
| NFR-9 **Observability** | Every run can be inspected after the fact: what was sent, what came back, what ran, how long it took and what it cost. |
| NFR-10 **Typed API** | The public Python API is fully typed and passes pyright strict. |

## Data persisted in M1 and secret handling

This section is the canonical statement of the gap. Other documents link here.

**What M1 persists verbatim** (in event payloads, in memory or in SQLite):
- Run `input` and `instructions`.
- The model name.
- Model response content and token usage.
- Failure `error_type` and `message`. The message is `str()` of the provider exception, unsanitized.

**What M1 does *not* guarantee:**
- Arbitrary user input can contain secret-like values (API keys, passwords, tokens, personal data). M1 stores them unchanged.
- Model responses and provider error messages can echo them back.
- `extra="forbid"` on payload models only rejects *undeclared fields*. It does not detect secrets embedded in string values.
- M1 makes **no** claim that secrets cannot enter persisted events.
- What M1 does have: provider credentials aren't part of `ModelRequest`, `RunCreated` or any other payload, so the runtime itself never writes them. The only provider is a scripted fake with no credentials.
- SQLite files are created with mode `0600` but are **not encrypted at rest**.
- There is no retention or deletion mechanism. The event log is append-only and enforced by database triggers.

**Required in M2, before real providers handle real data:**
1. **Credential references.** Configuration and events refer to credentials by name or reference, resolved at call time. Raw credentials never appear in run configuration, `ModelRequest` or events.
2. **Redaction before persistence and logging.** A redaction step applies to event payloads and log records before they are written.
3. **Sanitized provider errors.** Adapters sanitize exception messages before they become `RunFailed.message` or log output.
4. **Proof by tests.** Tests plant known secret values (in credentials, inputs and provider errors) and assert they don't appear in persisted events or captured logs.
5. **An explicit retention and deletion policy.** It must define how long run data is kept and how it's deleted, and how deletion squares with the append-only log (for example, deleting a run's log as a whole, or crypto-shredding). It's recorded as an ADR.
