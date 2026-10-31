# ADR-0004: Local-first persistence, SQLite as the initial store

- Status: Accepted (2026-10-31, end of M1). Revised from the proposal: no WAL.
- Date: 2026-10-31

## Context

Runs must survive process crashes (NFR-2) and be inspectable afterwards (NFR-9). Everything must work on one machine (NFR-8), with a path to cloud deployment later. The run record is append-heavy, ordered and read mostly per run.

## Decision

- Persistence goes through the `RunStore` port (`hollis.runtime.store`, ADR-0002). Every adapter passes one shared contract suite.
- The durable adapter is `SqliteRunStore`, using the **standard library `sqlite3`**. No new dependency, no ORM, no migration framework.
- **One connection per operation.** Each operation opens a connection inside `asyncio.to_thread`, uses it and closes it. Connections never cross threads or operations.
- **Transactions.** Appends run in `BEGIN IMMEDIATE`. The version check, the event-id check and every insert in a batch commit or roll back together. Lock waits are bounded by a busy timeout (default 5 s), and exceeding it raises `StoreUnavailable`.
- **Schema.**
  - One `STRICT` `events` table, keyed by `(run_id, sequence)`, with a unique `event_id`.
  - Triggers reject `UPDATE` and `DELETE`, so the log is append-only at the database level.
  - Requires SQLite 3.37 or newer, checked at open.
- **Migrations.**
  - Ordered statement lists, tracked in `PRAGMA user_version`, applied in one `BEGIN IMMEDIATE` transaction.
  - A database newer than the code is refused (`SchemaTooNew`).
- **Default rollback-journal mode.** The proposal said WAL. It's dropped because nothing in M1 needs concurrent readers during writes, and it adds `-wal`/`-shm` files and different durability semantics. Adopt it only with a measured need.
- New database files are created with mode `0600`. Data is **not encrypted at rest**. Run input and instructions are stored verbatim. The runtime redacts failure messages and registered secrets before they reach the store ([ADR-0009](0009-credentials-and-redaction.md)), and the store deletes nothing ([ADR-0010](0010-retention-and-deletion.md)). See the M1 data and secret-handling gap in [requirements](../product/requirements.md#data-persistence-and-secret-handling).
- An in-memory adapter exists for tests and ephemeral runs.

## Alternatives considered

- **JSONL files per run**: trivially inspectable, but weak atomicity guarantees, no querying across runs and hand-rolled locking.
- **PostgreSQL from the start**: requires an external service and breaks NFR-8. It becomes an adapter when cloud deployment is concrete.
- **An ORM or migration framework** (SQLAlchemy, Alembic): heavy for a single append-oriented table.
- **A long-lived shared connection**: cheaper per operation, but it needs `check_same_thread=False` plus locking discipline. Revisit only if connection cost is measured to matter.

## Trade-offs

- SQLite gives transactional appends, crash safety and ad-hoc querying with no service to run. Its limits are a single writer per database and the fact that it isn't shared across hosts.
- Connection-per-operation costs a connect per call. That's accepted for M1 and hasn't been measured.
- Raw `sqlite3` means owning SQL and migrations by hand.

## Consequences and evidence (M1)

- The same contract suite (`tests/adapters/test_store_contract.py`) passes for both adapters. It covers OCC, duplicate ids, atomic batches, concurrent appends and Hypothesis round-trips.
- Crash injection at every runtime boundary, plus a `SIGKILL` of a real process, leaves a consistent, resumable record (`tests/recovery/`). This is process-death evidence only. Power loss and OS crashes aren't simulated.
- Migration behavior is tested: fresh, re-open, pending migrations, failed-migration rollback and too-new schema (`tests/adapters/test_sqlite_store.py`).
- **Assumptions:** single process, a single active runner per run, a local file system (not network filesystems). Multi-process or cloud use needs a networked adapter and leases, both future work.
