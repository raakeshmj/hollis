# Testing and Evaluation Strategy

## Philosophy

- **Deterministic checks first.** Formatting, linting, strict type checking and tests run through `make check`, locally and in CI. A change is not done until `make check` passes. Never report a check as passing without running it.
- **Test behavior, not implementation.** Tests go through public interfaces. A refactor that keeps behavior shouldn't break tests.
- **Fakes over mocks.** Ports get real in-memory or scripted adapters (fake provider, in-memory store) that are shared across tests. Patching internals is a last resort.
- **Invariants are tests.** Architectural rules that matter (dependency direction, no vendor imports in the core, optional SDKs not loaded by the core, registered secrets never persisted) are automated tests, not prose.
- **No network in the default suite.** Provider adapters are tested through a real SDK client over a mock transport. Tests that need a real provider live in `tests/live/`, carry the `live` marker, and are skipped unless explicitly enabled. Mock-based tests don't verify live behaviour.
- **Agent evaluation waits for an agent.** We don't build benchmark infrastructure before there is a runtime worth evaluating.

## Test layers

| Layer | What | When |
|---|---|---|
| Unit | Pure logic: state transitions, fold, serialization (incl. Hypothesis properties) | From M1 |
| Contract | One shared test suite every adapter of a port must pass (for example, in-memory and SQLite `RunStore`) | From M1 |
| Integration | Full runs through the runtime with the scripted fake provider, incl. crash injection | From M1 |
| Architecture | Import-boundary tests (AST-based, stdlib only) | From M1 |
| Context and memory evals | Multi-session scenarios and compaction fidelity, model-free context sufficiency against baselines ([evaluation](evaluation.md)) | ✅ M4 (offline) |
| Platform journeys | The service layer every interface uses, driven by the deterministic demo model: runs, approvals, knowledge, memory, sessions, recovery (`tests/platform/`) | ✅ M6 |
| API, SDK and CLI | REST contract, errors and journeys in process; a real loopback server with live SSE and restart (`tests/server/`); every SDK operation against both backends (`tests/sdk/`); CLI output, JSON and exit codes (`tests/cli/`); examples (`tests/test_examples.py`) | ✅ M6 |
| Web app | TypeScript strict, ESLint with strict accessibility rules, Vitest component tests (`make web-check`); the committed build and its API contract (`tests/ui/`); Playwright journeys in the installed Chrome against a real server (opt-in: `make web-e2e`, `HOLLIS_UI_TESTS=1`; a CI job) | ✅ M6 |
| Orchestration | Delegation through the platform: parallel and dependent work, plans, approvals, cascade, limits, budgets, failures, recovery (`tests/platform/test_orchestration*.py`); adversarial agents (`tests/security/test_orchestration_security.py`); multi-agent vs single-agent evaluation with simulated agents ([orchestration-evaluation](orchestration-evaluation.md), `tests/evals/orchestration/`) | ✅ M7 (offline) |
| Specialization | Capability tools and their protections, adversarial git configurations (`tests/tools/`); catalog, materialization, readiness, contracts, skills (`tests/specialists/`); template agents, overlays, delegation inheritance, every first-party flow on the demo model (`tests/platform/test_specialists.py`); all interfaces and a Chrome journey; specialist vs generalist evaluation under explicit error models ([specialist-evaluation](specialist-evaluation.md), `tests/evals/specialists/`) | ✅ M8 (offline) |
| Behavioral evals | Assertions on agent trajectories against real models | Pending live access (opt-in memory, RAG, orchestration and specialist evals written, not run) |
| End-to-end benchmarks | Task-level success on realistic suites | Later |

## Reliability and security properties to verify

Each property maps to a requirement and gets a test when its component exists. ✅ marks properties that are tested, with the milestone that added them. In M3, the protections that matter most were also mutation-checked: each was removed in turn and the suite failed.

| Property | Requirement | First tested |
|---|---|---|
| Illegal state transitions are rejected | NFR-1 | ✅ M1 `tests/runtime/` |
| Crash at any step boundary leaves the run resumable or explicitly failed | NFR-2, FR-4 | ✅ M1 `tests/recovery/` |
| A durably recorded response never reinvokes the provider; ambiguous outcomes retry under the same operation id | FR-4 | ✅ M1 (model calls), M3 (tools) |
| Folding a recorded log reproduces the same state; serialization round-trips | NFR-3 | ✅ M1 (Hypothesis) |
| Stale appends never succeed; batches are atomic; both stores pass one contract | NFR-2 | ✅ M1 `tests/adapters/` |
| Core modules import no adapter or vendor SDK | NFR-7 | ✅ M1 `tests/architecture/` |
| Budgets stop a run cleanly when exceeded | NFR-6 | ✅ M2 (attempts, deadline, timeout) |
| Ambiguous outcomes are never retried under the `fail` policy; a recorded response never triggers another request | FR-4 | ✅ M2 (fake and Anthropic mock) |
| Provider failures are classified from typed errors, status codes and transport types, never message text | ADR-0007 | ✅ M2 `tests/adapters/` |
| SDK environment variables cannot redirect requests, swap credentials or inject headers | NFR-11 | ✅ M2 (hostile-environment tests) |
| Routing policy refuses disallowed localities before persistence; requests reach only the resolved endpoint | NFR-11 | ✅ M2 `tests/gateway/` |
| Custom-CA TLS works, and fails without the CA, over a real socket | ADR-0013 | ✅ M2 `tests/network/` |
| Protocol conformance (offline loopback; live opt-in) | ADR-0012 | ✅ offline; live: none recorded ([matrix](model-compatibility.md)) |
| Registered secrets never appear in persisted events, the database file or logs | NFR-4 | ✅ M2 (planted-key tests). Arbitrary secrets in user text are not detected ([gap](../product/requirements.md#data-persistence-and-secret-handling)) |
| Tool arguments failing schema validation never execute | NFR-4, FR-6 | ✅ M3 `tests/runtime/test_tool_loop.py`, `tests/tools/` |
| Policy-denied tool calls never execute and are recorded as denied | FR-7 | ✅ M3 |
| No tool executes without an `allow` decision or an approval bound to the exact tool version and arguments; hand-built illegal logs are rejected by the fold | FR-7 | ✅ M3 `tests/runtime/test_tool_fold.py` |
| Nothing in a turn executes while any approval in it is pending; approvals survive restarts and can't be replayed onto other arguments | FR-7 | ✅ M3 |
| A started tool call with no result is re-executed only if idempotent, otherwise reconciled or recorded `ambiguous` | FR-4 | ✅ M3 `tests/recovery/test_tool_recovery.py` |
| Tool descriptions, MCP annotations and tool output cannot grant permission; external output is framed as untrusted | NFR-4 | ✅ M3 `tests/security/` |
| Registered secrets never persist via tool arguments, continuations or tool results; calls carrying them are denied | NFR-4 | ✅ M3 |
| Model turns, tool calls, parallelism, timeouts, output size and the deadline bound every run; no tool starts after the deadline | NFR-6 | ✅ M3 |
| Filesystem tools stay inside their root; the command tool runs no shell, refuses option injection and kills its process group | NFR-4 | ✅ M3 `tests/tools/` (not a sandbox) |
| MCP: only allowlisted tools are exposed; pinned or changed definitions are refused; real stdio and HTTP transports work | ADR-0017 | ✅ M3 offline; no third-party servers |
| Every attempt of a logical model call sends the request built from its recorded context plan | NFR-3, FR-4 | ✅ M4 `tests/runtime/test_context.py` |
| A plan cannot skip uncompacted turns; summaries cannot drop notes; the latest turn is never compacted | FR-8 | ✅ M4 `tests/runtime/test_context_fold.py` |
| Mandatory context that cannot fit fails the run before calling the model; optional memories are dropped and reported first | FR-8, NFR-6 | ✅ M4 |
| Compaction keeps every action and note across repeated lossy summaries | FR-8 | ✅ M4 `tests/evals/` |
| Large results are stored whole, referenced in context, readable only by their own run | FR-8, NFR-4 | ✅ M4 `tests/tools/test_artifacts.py` |
| Memory is isolated by scope at the store, manager, runner and fold; FTS syntax cannot widen scope | NFR-4 | ✅ M4 `tests/memory/`, `tests/security/` |
| Updates supersede, corrections retract, conflicts never overwrite, temporary conditions expire; as-of queries see the past | FR-9 | ✅ M4 `tests/memory/test_lifecycle.py` |
| Credentials are refused in memory and redacted from summaries and artifacts; recalled memories never authorize tools | NFR-4 | ✅ M4 |
| Deleted memories are gone from the store, index and file bytes; run-log copies remain (honest deletion) | NFR-4 | ✅ M4 |
| No retriever returns content outside the caller's collections or labels; requested collections are intersected with the grant | NFR-4, FR-15 | ✅ M5 `tests/security/test_knowledge_security.py` |
| Re-ingesting unchanged content is a no-op; replacing or deleting a source leaves no stale chunks, index rows or vectors | FR-15 | ✅ M5 `tests/knowledge/test_lifecycle.py` |
| Exact identifiers rank first despite fusion; embedding spaces are never mixed | FR-15, FR-16 | ✅ M5 |
| Hidden or invisible document content never reaches the index; instruction-like chunks stay out of automatic context; documents cannot forge citations, identity or trust | NFR-4 | ✅ M5 |
| Answers are checked against the run's actual evidence; fabricated citations are reported; external tools cannot supply evidence | FR-15 | ✅ M5 `tests/knowledge/test_rag_runtime.py` |
| Retrieval quality compared across configurations on a development and a held-out corpus | FR-15 | ✅ M5 offline ([rag-evaluation](rag-evaluation.md)); live: not run |
| Every network route except liveness requires authentication; tokens and sessions are hashed at rest; no token or session value appears in stored data or responses after real use | NFR-4, FR-18 | ✅ M6 `tests/server/test_secrets.py`, `tests/platform/test_auth.py` |
| Roles, run visibility, the approve permission and grant intersection are enforced in the services, so every interface inherits them (mutation-checked) | FR-18 | ✅ M6 `tests/platform/test_authorization.py` |
| An approval is recorded with the authenticated principal and only for a run visible to them; a cancelled run can no longer be approved | FR-7, FR-18 | ✅ M6 |
| Cookie requests that change state need this server's Origin; unexpected Host headers are refused; responses carry the CSP | FR-18 | ✅ M6 `tests/server/test_api.py` |
| Runs survive a dropped event stream, shutdown in the middle of a model call, cancellation and restart; the run index is re-derivable from the logs | NFR-2, FR-4 | ✅ M6 `tests/platform/test_runs.py`, `tests/sdk/`, `tests/server/test_live_server.py` |
| The platform, SDK and CLI load no optional dependency at import | NFR-7 | ✅ M6 `tests/architecture/` |
| A child run's memory and knowledge never exceed its agent's, its parent run's and its owner's current grants; a demoted owner cannot delegate; results pass only between delegations of the same parent (mutation-checked) | NFR-4, FR-12 | ✅ M7 `tests/platform/test_orchestration.py`, `tests/security/test_orchestration_security.py` |
| An interrupted delegation re-executes once and attaches to its existing child; no child is ever created twice; cancelling a run cancels its descendants; no child outlives its delegation | NFR-2, FR-12 | ✅ M7 `tests/platform/test_orchestration.py`, `tests/evals/orchestration/` |
| A run's policy overlay only tightens the workspace decision and every delegated run is held to its parent's rules first; file tools never read or write repository metadata or Hollis data and never write through links; git tools never run programs planted in repository configuration; a contract accepts only results whose references match the run's log | NFR-4 | ✅ M8 `tests/runtime/test_policy_overlay.py`, `tests/tools/`, `tests/specialists/`, `tests/platform/test_specialists.py` |
| A child's answer cannot forge evidence or escape its frame; evidence crosses runs only from recorded sources and is verified by the parent's grounding check (mutation-checked) | NFR-4 | ✅ M7 `tests/security/test_orchestration_security.py` |
| Depth, children, runs per tree and token budgets bound delegation; the fold rejects lineage beyond the recorded depth and calls beyond `max_total_tokens`; delegated work keeps the coordinator's reserve under concurrency | NFR-6 | ✅ M7 `tests/runtime/test_delegation.py`, `tests/platform/test_orchestration*.py` |

## Future behavioral evaluations (once live access is available)

Behavioral evals follow the approach in [research](../research/harness-engineering.md):

- Each eval targets **one observed failure mode** and asserts **discrete actions** in the trajectory (a tool was or wasn't called, a validator ran after an edit, a clarification was requested). They don't grade prose.
- Strict tool-milestone assertions for simple tasks. Outcome-based checks (possibly LLM-judged) for complex tasks.
- Evals are written as pytest tests over recorded trajectories, so they're cheap to run locally.
- Real-model evals are nondeterministic, so they run in batches and are tracked as **pass rates over time**. They don't block individual PRs.
- Regression triggers: prompt changes, tool-schema changes, provider or model upgrades.

## Future trajectory and end-to-end evaluation requirements

- Trajectories must be exportable from the event log with enough fidelity to assert tool calls, arguments, ordering, context inclusions, token usage and cost (FR-13).
- Runs must be reproducible from recorded model responses (record and replay), so a failing trajectory can become a deterministic regression test.
- End-to-end benchmark results are recorded with model, provider, Hollis version and configuration, so comparisons are meaningful.
- Evaluation tooling is built on the public SDK, so it also exercises the developer interface.
