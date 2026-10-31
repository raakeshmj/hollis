# ADR-0005: Run state derived from an append-only event log

- Status: Proposed. Validated by M1.
- Date: 2026-10-31

## Context

The priorities are correctness, recoverability and explicit state (NFR-1, NFR-2, NFR-5). Resume must not repeat completed side effects (FR-4). Behavioral evaluation needs structured trajectories of what an agent did (FR-13, see [research](../research/harness-engineering.md)). Inspection needs a complete history (NFR-9).

## Decision (proposed)

- Each run has an **append-only, ordered event log**: run created, state transitions, model request and response, tool call intent and result, budget events, errors.
- The current run state is a **pure function of the event log** (a fold). The runtime appends events and never mutates state in place.
- Side-effecting steps append an *intent* event before executing and a *result* event after. On resume, an intent without a result marks a step whose outcome is unknown, handled by policy (retry if idempotent, otherwise fail explicitly).
- Events are versioned, typed models. The schema is part of the public contract once released.

## Alternatives considered

- **Mutable run row plus a separate audit log**: two sources of truth that can diverge, and resume logic has to reconcile them.
- **Checkpointing full state snapshots only**: loses the trajectory needed for evals and debugging, and doesn't capture in-flight side effects.
- **Adopting an external durable-workflow engine**: strong guarantees, but heavy infrastructure, which conflicts with local-first and modular-monolith constraints.

## Trade-offs

- Gains: a single source of truth, deterministic replay with a fake provider, trajectories for free, and natural audit.
- Costs: event schema design and versioning discipline, and log growth (snapshots can be added later as an optimization without changing semantics).

## Consequences / validation in M1

- Property: replaying a recorded log reproduces the same state (tested).
- Crash-injection tests at every step boundary.
- If the fold becomes unworkable for real flows, revise before M2.
