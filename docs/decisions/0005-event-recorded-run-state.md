# ADR-0005: Run state derived from an append-only event log

- Status: Accepted (2026-10-31, end of M1), with the refinements below.
- Date: 2026-10-31

## Context

The priorities are correctness, recoverability and explicit state (NFR-1, NFR-2, NFR-5). Resume must not repeat work that is durably recorded (FR-4). Behavioral evaluation needs structured trajectories (FR-13, see [research](../research/harness-engineering.md)). Inspection needs a complete history (NFR-9).

## Decision

- **Append-only log.** Each run has an ordered event log of `EventEnvelope` records (`hollis.runtime.events`). Each record has: `event_id` (unique store-wide), `run_id`, `sequence` (contiguous from 1, equal to the run version), `schema_version`, `occurred_at` (UTC) and a typed `payload` discriminated by `type`.
- **Vocabulary.** M1 has only the events the runtime emits: `RunCreated`, `ModelCallStarted`, `ModelResponseRecorded`, `RunCompleted`, `RunFailed`. New event types are added only when a behavior needs them.
- **State is a pure fold.** `apply` and `fold` in `hollis.runtime.state` derive the state from the log, using an explicit transition table. Invalid logs raise `InvalidEventSequence`: gaps, reorders, mixed runs, duplicate ids, illegal transitions, and operation-id or attempt mismatches. Nothing is skipped.
- **Validate before persisting.** The runtime builds each event, validates it with `apply` against the current state, and only then appends it.
- **Optimistic concurrency.** `append(run_id, expected_version, events)` is atomic and always fails on a stale version. That's the only coordination mechanism in M1; there are no leases.
- **Intent per attempt.** Each physical model attempt appends a `ModelCallStarted(operation_id, attempt)` intent *before* invoking the provider. Execution semantics are in ADR-0006.
- **Schema evolution.**
  - Payload versions are per event type.
  - Changing a payload's shape means bumping its version and adding an upcaster. Stored events are never rewritten.
  - An unknown type or version raises `UnknownEventSchema`.
  - M1 has version 1 only. The registry exists, with no upcasters and no snapshots.

## Alternatives considered

- **Mutable run row plus a separate audit log**: two sources of truth that can diverge.
- **Snapshots only**: loses the trajectory, and doesn't capture in-flight side effects.
- **An external durable-workflow engine**: heavy infrastructure, conflicting with local-first and the modular monolith.
- **Store-allocated sequences**: the caller allocating `expected_version + n` keeps events fully determined before persistence, so they can be validated by the fold first. The store still rejects anything that isn't contiguous.

## Trade-offs

- Gains: one source of truth, deterministic replay, trajectories for free, explicit concurrency failures.
- Costs: discipline about event schemas and versioning, and log growth (snapshots can be added later without changing semantics). The strict fold means that any future relaxation must be deliberate.

## Consequences and evidence (M1)

- The transition table is tested exhaustively, covering all 30 status × event pairs (`tests/runtime/test_transitions.py`).
- Hypothesis tests run over generated logs (`tests/runtime/test_fold.py`, `test_event_serialization.py`):
  - Valid logs fold.
  - Every prefix is valid.
  - `fold` equals iterated `apply`.
  - Structural and operation/attempt mutations are rejected.
  - Serialization round-trips.
- Crash recovery is tested at every boundary against both stores (`tests/recovery/`).
- Open for M3: multi-turn and tool events will extend the vocabulary and transition table, with expected revisions to the state shape.
