# ADR-0003: Async-first runtime core

- Status: Accepted
- Date: 2026-10-31

## Context

Agent runs are dominated by I/O wait: model calls (seconds each), tool execution (subprocesses, HTTP) and storage. The platform must support cancellation, timeouts and budgets (NFR-6), parallel tool calls and, later, several concurrent agents (FR-12), plus a REST API (FR-11). Most current Python model SDKs and web frameworks are async-native.

## Decision

- The runtime core and the I/O ports (model, storage, tool execution) are `async`.
- Pure logic (state transitions, validation, budget arithmetic, context selection) stays **synchronous**. Async is used only where it buys concurrency or cancellation.
- Use the standard library `asyncio`, with structured concurrency (`asyncio.TaskGroup`, `asyncio.timeout`).
- Blocking libraries (for example, stdlib `sqlite3`) are wrapped with `asyncio.to_thread` inside adapters.
- A synchronous convenience wrapper for scripts may be offered in the SDK, but it's not a second implementation.

## Alternatives considered

- **Synchronous core with threads**: simpler to debug, but cancellation and timeouts are unreliable, and concurrency across many runs is costly.
- **Sync and async dual APIs throughout**: doubles the surface and the test matrix.
- **AnyIO or Trio**: AnyIO adds backend portability we don't need yet. Revisit if a dependency requires it.

## Trade-offs

- Async adds "function colouring" and harder stack traces. Containing it to I/O paths limits the spread.
- Gains: first-class cancellation and timeouts, cheap concurrency, a natural fit with async web and model SDKs.

## Consequences

- M1 adds an async test runner. The choice between pytest-asyncio and a minimal in-house fixture is made in M1, and any new dependency must be justified there.
- Ruff `ASYNC` rules are enabled to catch blocking calls inside coroutines.
