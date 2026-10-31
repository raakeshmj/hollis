# ADR-0002: Vendor-neutral core with ports and adapters

- Status: Accepted
- Date: 2026-10-31

## Context

Hollis must be model-agnostic (FR-5, NFR-7) and storage-agnostic, and runs must be testable without network access (NFR-3). Model vendors change APIs and features often. Coupling the runtime to any one vendor's types would spread that churn through the codebase.

## Decision

- The runtime core defines **ports**: interfaces plus Hollis-owned data types for model calls (messages, tool calls, usage), run storage and, later, tool execution.
- **Adapters** implement ports and may import vendor SDKs. The core never imports adapters or vendor SDKs, and adapters depend on the core.
- Every port ships with a deterministic in-process adapter (scripted fake provider, in-memory store) used in tests.
- Ports are introduced only when a second implementation exists or is imminent. The fake counts, so M1 introduces ports for both model and storage.

## Alternatives considered

- **Build on one vendor SDK's types**: fastest initially, but it violates vendor neutrality and makes testing depend on vendor mocks.
- **Adopt an existing multi-provider abstraction library as the core interface**: it outsources our most important boundary to a dependency we don't control. It could be wrapped inside an adapter later.
- **Plugin system with dynamic discovery**: speculative. Explicit registration is enough until third-party adapters exist.

## Trade-offs

- Each provider feature has to be mapped into Hollis types, which costs work and may lag vendors. Provider-specific escape hatches will be needed (an open question for M2).
- In return, provider swaps need no agent changes, the core is testable offline, and vendor churn stays in adapters.

## Consequences

- From M1, an AST-based test fails if any core module imports an adapter module or a known vendor SDK.
- Contract test suites run every adapter of a port against the same expectations.
