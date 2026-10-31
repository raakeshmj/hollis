"""SQLite RunStore using the standard-library `sqlite3` module (ADR-0004).

Connection lifecycle: every operation opens its own connection inside a worker thread
(`asyncio.to_thread`), uses it, and closes it before returning. Connections are never shared
between threads or operations. `:memory:` is unsupported because each connection would see
a separate database.

Transactions: writes run in `BEGIN IMMEDIATE`, which takes the write lock up front, so the
version check and inserts are atomic with respect to other writers. Lock waits are bounded by
`busy_timeout`; exceeding it raises `StoreUnavailable`.

Assumptions (M1): a single process with a single active runner per run, on a local file
system. SQLite's default rollback-journal mode is used; WAL is not enabled without a
measured need. Data is not encrypted at rest; new database files are created with mode 0600.
"""

import asyncio
import contextlib
import json
import os
import sqlite3
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any, Self
from uuid import UUID

from hollis.runtime.events import EventEnvelope, UnknownEventSchema, event_from_dict
from hollis.runtime.store import (
    ConcurrencyConflict,
    CorruptEvent,
    DuplicateEventId,
    RunNotFound,
    StoreError,
    StoreUnavailable,
    duplicate_within,
    validate_batch,
)

MIN_SQLITE_VERSION = (3, 37, 0)  # STRICT tables

# Index i upgrades the schema from user_version i to i + 1. Append only; never edit.
MIGRATIONS: Sequence[Sequence[str]] = (
    (
        """
        CREATE TABLE events (
            run_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK (sequence >= 1),
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (run_id, sequence)
        ) STRICT
        """,
        """
        CREATE TRIGGER events_no_update BEFORE UPDATE ON events
        BEGIN SELECT RAISE(ABORT, 'events are append-only'); END
        """,
        """
        CREATE TRIGGER events_no_delete BEFORE DELETE ON events
        BEGIN SELECT RAISE(ABORT, 'events are append-only'); END
        """,
    ),
)

_COLUMNS = "event_id, run_id, sequence, event_type, schema_version, occurred_at, payload"


class SchemaTooNew(StoreError):
    def __init__(self, found: int, supported: int) -> None:
        super().__init__(f"database schema version {found} is newer than supported {supported}")


class SqliteRunStore:
    def __init__(self, path: Path, busy_timeout: float) -> None:
        """Use `await SqliteRunStore.open(...)`, which also initializes the schema."""
        self._path = path
        self._busy_timeout = busy_timeout

    @classmethod
    async def open(cls, path: str | os.PathLike[str], *, busy_timeout: float = 5.0) -> Self:
        store = cls(Path(path), busy_timeout)
        await asyncio.to_thread(store._initialize)
        return store

    async def append(
        self, run_id: UUID, expected_version: int, events: Sequence[EventEnvelope]
    ) -> None:
        validate_batch(run_id, expected_version, events)
        await asyncio.to_thread(self._append, run_id, expected_version, events)

    async def read(self, run_id: UUID) -> list[EventEnvelope]:
        return await asyncio.to_thread(self._read, run_id)

    # --- synchronous operations, each run in a worker thread with its own connection ------

    def _initialize(self) -> None:
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise StoreUnavailable(f"SQLite >= 3.37 required, found {sqlite3.sqlite_version}")
        with contextlib.suppress(FileExistsError):
            os.close(os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        with self._transaction() as conn:
            current: int = conn.execute("PRAGMA user_version").fetchone()[0]
            if current > len(MIGRATIONS):
                raise SchemaTooNew(current, len(MIGRATIONS))
            for target in range(current + 1, len(MIGRATIONS) + 1):
                for statement in MIGRATIONS[target - 1]:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {int(target)}")  # PRAGMA can't bind params

    def _append(self, run_id: UUID, expected_version: int, events: Sequence[EventEnvelope]) -> None:
        with self._transaction() as conn:
            current = self._version(conn, run_id)
            if current != expected_version:
                raise ConcurrencyConflict(run_id, expected_version, current)
            if (dup := duplicate_within(events)) is not None:
                raise DuplicateEventId(dup)
            if (existing := self._first_existing_id(conn, events)) is not None:
                raise DuplicateEventId(existing)
            try:
                conn.executemany(
                    f"INSERT INTO events ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",  # noqa: S608  # constant column list
                    [_to_row(event) for event in events],
                )
            except sqlite3.IntegrityError as exc:
                # The pre-checks above run under the write lock, so key violations cannot occur
                # here; anything else (e.g. a trigger) aborts the whole batch.
                raise StoreError(f"append rejected by database: {exc}") from exc

    def _read(self, run_id: UUID) -> list[EventEnvelope]:
        with contextlib.closing(self._connect()) as conn:
            try:
                rows: list[tuple[Any, ...]] = conn.execute(
                    f"SELECT {_COLUMNS} FROM events WHERE run_id = ? ORDER BY sequence",  # noqa: S608  # constant column list
                    (str(run_id),),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                raise StoreUnavailable(str(exc)) from exc
        if not rows:
            raise RunNotFound(run_id)
        return [_from_row(row) for row in rows]

    # --- helpers --------------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: no implicit transactions; we issue BEGIN/COMMIT explicitly.
        return sqlite3.connect(self._path, timeout=self._busy_timeout, isolation_level=None)

    @contextlib.contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        except sqlite3.OperationalError as exc:
            raise StoreUnavailable(str(exc)) from exc
        finally:
            conn.close()

    @staticmethod
    def _version(conn: sqlite3.Connection, run_id: UUID) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM events WHERE run_id = ?", (str(run_id),)
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _first_existing_id(
        conn: sqlite3.Connection, events: Sequence[EventEnvelope]
    ) -> UUID | None:
        ids = [str(event.event_id) for event in events]
        placeholders = ", ".join("?" * len(ids))
        row = conn.execute(
            f"SELECT event_id FROM events WHERE event_id IN ({placeholders}) LIMIT 1",  # noqa: S608  # placeholders only
            ids,
        ).fetchone()
        return UUID(row[0]) if row else None


def _to_row(event: EventEnvelope) -> tuple[str, str, int, str, int, str, str]:
    data = event.model_dump(mode="json")
    return (
        str(event.event_id),
        str(event.run_id),
        event.sequence,
        event.event_type.value,
        event.schema_version,
        data["occurred_at"],
        json.dumps(data["payload"], separators=(",", ":")),
    )


def _from_row(row: tuple[Any, ...]) -> EventEnvelope:
    event_id, run_id, sequence, event_type, schema_version, occurred_at, payload = row
    try:
        event = event_from_dict(
            {
                "event_id": event_id,
                "run_id": run_id,
                "sequence": sequence,
                "schema_version": schema_version,
                "occurred_at": occurred_at,
                "payload": json.loads(payload),
            }
        )
    except UnknownEventSchema:
        raise
    except ValueError as exc:
        raise CorruptEvent(f"event {event_id}: {exc}") from exc
    if event.event_type.value != event_type:
        raise CorruptEvent(f"event {event_id}: column type {event_type!r} != payload type")
    return event
