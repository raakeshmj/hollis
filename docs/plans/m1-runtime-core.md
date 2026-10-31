# M1 Plan: Runtime Core

Status: proposed, not started. Re-validate this plan in Plan Mode before implementing.

## Goal

A minimal runtime that executes a run against a **scripted fake model provider**. It holds explicit, validated state, persists every step to an append-only event log, and resumes correctly after a crash. No real providers, tools, memory or interfaces beyond the SDK.

## Why this first

The runtime and harness are the platform's foundation. Correctness, recoverability and explicit state are the top priorities. They are cheapest to get right, and to test deterministically, before real models, tools and networks add nondeterminism. M1 also validates the two proposed ADRs (0004 SQLite, 0005 event-recorded state) before anything builds on them.

## Scope

| In | Out (later milestone) |
|---|---|
| Pydantic v2 domain types: run config, events, model messages | Real model providers (M2) |
| Run state machine with validated transitions | Tools and sandboxing (M3) |
| Append-only event log; state as a fold over events | Context management and memory (M4) |
| `ModelProvider` port + scripted fake adapter | CLI and REST API beyond `--version` (M5) |
| `RunStore` port + in-memory and SQLite (stdlib) adapters | Budgets beyond a step limit (M2) |
| Async step loop: model turn until a terminal response or the step limit | Streaming (M2) |
| Resume from the event log | Multi-agent (M6) |
| Import-boundary architecture tests | |

## Expected layout (create only what's used)

```
src/hollis/
  runtime/      # state machine, events, step loop, ports (core)
  adapters/     # fake provider, in-memory store, sqlite store
tests/
  runtime/ adapters/ architecture/
```

The final module names get settled while implementing. The rule is fixed: `hollis.runtime` must not import `hollis.adapters` or any vendor SDK.

## Dependencies

- **Pydantic v2** (runtime): typed, validated events and config. This is a product constraint.
- **Async testing**: either pytest-asyncio or a small in-house fixture using `asyncio.run`. Pick the smaller option that supports the needed fixtures, and justify it in the commit.
- No ORM or migration library. SQLite schema versioning uses `PRAGMA user_version`.

## Steps (each a verified commit)

1. Domain types and the state machine, with exhaustive transition tests.
2. Event types and the state fold; property test that replaying events reproduces the state.
3. `RunStore` port, in-memory adapter and a shared contract test suite.
4. SQLite adapter passing the same contract suite, plus a migration mechanism.
5. `ModelProvider` port and scripted fake adapter.
6. Async step loop wiring the ports together; integration tests with the fake.
7. Resume: crash-injection tests at each step boundary, for both stores.
8. Architecture tests for import boundaries.
9. Update the ADR-0004 and ADR-0005 status, the overview's decided/open table, and the CLAUDE.md map.

## Acceptance criteria

- `make check` passes, with pyright strict at 0 errors.
- Every illegal state transition is rejected (exhaustive test).
- Replaying any recorded event log yields an identical state (tested).
- The same contract suite passes for the in-memory and SQLite stores.
- For a crash injected at every step boundary, the run resumes to the same final state as an uninterrupted run, and no completed model step is re-requested from the provider.
- An architecture test fails if `hollis.runtime` imports `hollis.adapters` or a vendor SDK. Verify by temporarily adding an offending import.
- ADR-0004 and ADR-0005 are moved to Accepted or revised, with evidence.

## Risks

- **Over-designing the event schema before real providers.** Mitigation: keep events minimal and versioned, and expect revision in M2.
- **Async test flakiness.** Mitigation: the fake provider is fully deterministic, with no sleeps or real time in tests.
- **SQLite crash semantics hard to test in-process.** Mitigation: inject failures at adapter boundaries, plus one subprocess kill test.
