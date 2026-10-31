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
| Behavioral evals | Assertions on agent trajectories against real models | Pending live access |
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
