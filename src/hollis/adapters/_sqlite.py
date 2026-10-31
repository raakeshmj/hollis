"""Shared plumbing for the SQLite artifact and memory stores (same conventions as ADR-0004).

Each store lives in its own database file with its own `PRAGMA user_version` migrations. Every
operation opens a connection in a worker thread and closes it before returning. New files are
created with mode 0600. `secure_delete` is enabled so deleted content is overwritten in the
database file (it may still exist in backups, the rollback journal during a transaction, or the
file system's own copies; see ADR-0021).
"""

import contextlib
import os
import sqlite3
from collections.abc import Generator, Sequence
from pathlib import Path

from hollis.runtime.store import StoreError, StoreUnavailable

MIN_SQLITE_VERSION = (3, 37, 0)


class SchemaTooNew(StoreError):
    def __init__(self, found: int, supported: int) -> None:
        super().__init__(f"database schema version {found} is newer than supported {supported}")


class SqliteFile:
    def __init__(self, path: Path, busy_timeout: float) -> None:
        self.path = path
        self._busy_timeout = busy_timeout

    def initialize(self, migrations: Sequence[Sequence[str]]) -> None:
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise StoreUnavailable(f"SQLite >= 3.37 required, found {sqlite3.sqlite_version}")
        with contextlib.suppress(FileExistsError):
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        with self.transaction() as conn:
            current: int = conn.execute("PRAGMA user_version").fetchone()[0]
            if current > len(migrations):
                raise SchemaTooNew(current, len(migrations))
            for target in range(current + 1, len(migrations) + 1):
                for statement in migrations[target - 1]:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {int(target)}")  # PRAGMA can't bind params

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self._busy_timeout, isolation_level=None)
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextlib.contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        try:
            conn = self.connect()
        except sqlite3.OperationalError as exc:
            raise StoreUnavailable(str(exc)) from exc
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

    @contextlib.contextmanager
    def reading(self) -> Generator[sqlite3.Connection]:
        try:
            conn = self.connect()
        except sqlite3.OperationalError as exc:
            raise StoreUnavailable(str(exc)) from exc
        try:
            yield conn
        except sqlite3.OperationalError as exc:
            raise StoreUnavailable(str(exc)) from exc
        finally:
            conn.close()
