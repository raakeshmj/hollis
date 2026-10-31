# ADR-0010: Run data retention and deletion policy

- Status: Accepted (policy only). The mechanism for runs is deferred. Deletion of derived memories is implemented separately ([ADR-0021](0021-memory-storage-retrieval-and-deletion.md)), and artifacts can be deleted per run ([ADR-0019](0019-artifact-store.md)).
- Date: 2026-10-31

## Context

Runs persist user input, instructions, model responses and failure messages (requirements: "Data persistence and secret handling"). Redaction (ADR-0009) can't catch every sensitive value, so operators need a way to bound how long run data lives and to delete it on request. The event log is append-only by design (ADR-0005). The SQLite store enforces this with triggers that reject `UPDATE` and `DELETE`.

## Decision (policy)

1. **The unit of deletion is a whole run.**
   - A run's entire event log is deleted atomically.
   - Individual events are never edited or removed.
   - Partial deletion would make the fold reject the log, or worse, reconstruct a state that never existed.
2. **Retention is operator-configured.**
   - Hollis doesn't choose a default retention period for operators.
   - Without configuration, data is kept until deleted.
   - A configured period applies to *terminal* runs measured from their final event. Non-terminal runs are never deleted automatically, because deletion would erase recovery state.
3. **Deletion is explicit and auditable.** Any future deletion operation records which run was deleted and when, outside the deleted log, without retaining its content.
4. **No in-place redaction of stored events.** If content must be removed, the whole run is deleted.

## Mechanism (deferred)

- **Not implemented in M2.** There's no `RunStore.delete_run`, and the SQLite triggers still forbid all deletes.
- Tracked as an explicit requirement (requirements: "Data persistence and secret handling").
- The expected shape: a `delete_run(run_id)` contract operation, implemented and contract-tested on every store. A trigger that permits only whole-run deletion, for example via a controlled tombstone table. A retention sweeper invoked by the operator.
- **Until then, the operator's only option is deleting the database file.**

## Alternatives considered

- **Crypto-shredding** (per-run keys, destroyed to delete): fits append-only storage well, but requires encryption at rest, which Hollis doesn't have. Revisit together with encryption.
- **Soft delete (tombstones only):** leaves the content on disk, so it doesn't meet the purpose.
- **Implementing deletion in M2:** deliberately deferred to keep M2 focused on the provider integration (decided 2026-10-31).

## Trade-offs

- Whole-run deletion is simple and keeps every remaining log valid, but can't remove one sensitive field while keeping the rest of a run's history.
- Never deleting non-terminal runs automatically protects recovery, but an abandoned in-flight run is kept until an operator acts.
- Deferring the mechanism keeps M2 small. The cost is that the policy is unenforceable in code until it lands.

## Consequences

- Until the mechanism exists, deployments handling sensitive data must manage retention at the file level, and the docs say so.
- The mechanism's milestone is open. It must land before any multi-user or hosted deployment.
