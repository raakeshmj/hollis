# ADR-0004: Local-first persistence, SQLite as the initial store

- Status: Proposed. Accept or revise at the end of M1.
- Date: 2026-10-31

## Context

Runs must survive process crashes (NFR-2) and be inspectable afterwards (NFR-9). Everything must work on one machine (NFR-8), with a path to cloud deployment later. The run record is append-heavy, ordered and read mostly per run.

## Decision (proposed)

- Persistence goes through a `RunStore` port (ADR-0002).
- The first durable adapter uses **SQLite via the standard library `sqlite3`** module (WAL mode), wrapped for async use. That's zero new dependencies.
- An in-memory adapter exists for tests and ephemeral runs.

## Alternatives considered

- **JSONL files per run**: trivially inspectable, but weak atomicity guarantees, no querying across runs and hand-rolled locking.
- **PostgreSQL from the start**: strong, but it requires an external service and breaks NFR-8 for local use. It becomes an adapter when cloud deployment is concrete.
- **An ORM** (SQLAlchemy and similar): a heavy dependency for a small, append-oriented schema. Revisit if the schema grows.

## Trade-offs

- SQLite gives transactional appends, crash safety and ad-hoc querying with no service to run. Its limits are a single writer per database and the fact that it isn't shared across hosts.
- Raw `sqlite3` means owning migrations and SQL by hand.

## Consequences / validation in M1

To be accepted, M1 must show:
- The same contract test suite passes for the in-memory and SQLite adapters.
- A crash-injection test (process killed between event writes) leaves a consistent, resumable record.
- The migration approach (for example, `PRAGMA user_version` plus ordered scripts) is in place.

If any of these fail, revise this ADR before building on it.
