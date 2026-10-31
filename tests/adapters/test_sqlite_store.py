"""SQLite-specific behaviour beyond the shared RunStore contract."""

import contextlib
import sqlite3
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

import pytest

from hollis.adapters import sqlite_store
from hollis.adapters.sqlite_store import MIGRATIONS, SchemaTooNew, SqliteRunStore
from hollis.runtime.events import UnknownEventSchema
from hollis.runtime.store import CorruptEvent, RunNotFound, StoreError, StoreUnavailable
from tests.support.events import build_log, happy_payloads


def _query(path: Path, sql: str) -> list[tuple[object, ...]]:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchall()


def _execute(path: Path, sql: str) -> None:
    with contextlib.closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute(sql)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "runs.db"


async def test_fresh_database_is_migrated_to_latest(db: Path) -> None:
    await SqliteRunStore.open(db)
    assert _query(db, "PRAGMA user_version") == [(len(MIGRATIONS),)]
    assert _query(db, "SELECT name FROM sqlite_master WHERE type = 'table'") == [("events",)]


async def test_reopening_is_a_noop_and_keeps_data(db: Path) -> None:
    run = uuid4()
    log = build_log(run, happy_payloads(run))
    await (await SqliteRunStore.open(db)).append(run, 0, log)
    reopened = await SqliteRunStore.open(db)
    assert _query(db, "PRAGMA user_version") == [(len(MIGRATIONS),)]
    assert await reopened.read(run) == log


async def test_newer_schema_is_refused(db: Path) -> None:
    await SqliteRunStore.open(db)
    _execute(db, "PRAGMA user_version = 99")
    with pytest.raises(SchemaTooNew):
        await SqliteRunStore.open(db)


async def test_pending_migrations_apply_in_order(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await SqliteRunStore.open(db)
    extra: Sequence[Sequence[str]] = (*MIGRATIONS, ("CREATE TABLE extra (x INTEGER) STRICT",))
    monkeypatch.setattr(sqlite_store, "MIGRATIONS", extra)
    await SqliteRunStore.open(db)
    assert _query(db, "PRAGMA user_version") == [(len(extra),)]
    assert ("extra",) in _query(db, "SELECT name FROM sqlite_master WHERE type = 'table'")


async def test_failed_migration_rolls_back_entirely(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await SqliteRunStore.open(db)
    broken: Sequence[Sequence[str]] = (
        *MIGRATIONS,
        ("CREATE TABLE half (x INTEGER) STRICT", "THIS IS NOT SQL"),
    )
    monkeypatch.setattr(sqlite_store, "MIGRATIONS", broken)
    with pytest.raises(StoreUnavailable):
        await SqliteRunStore.open(db)
    assert _query(db, "PRAGMA user_version") == [(len(MIGRATIONS),)]
    assert ("half",) not in _query(db, "SELECT name FROM sqlite_master WHERE type = 'table'")


@pytest.mark.parametrize("statement", ["UPDATE events SET payload = '{}'", "DELETE FROM events"])
async def test_events_table_is_append_only(db: Path, statement: str) -> None:
    run = uuid4()
    await (await SqliteRunStore.open(db)).append(run, 0, build_log(run, happy_payloads(run)))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        _execute(db, statement)
    assert len(_query(db, "SELECT * FROM events")) == 4


async def test_database_rejection_mid_batch_rolls_back_whole_batch(db: Path) -> None:
    store = await SqliteRunStore.open(db)
    _execute(
        db,
        "CREATE TRIGGER reject_third BEFORE INSERT ON events WHEN NEW.sequence = 3 "
        "BEGIN SELECT RAISE(ABORT, 'rejected'); END",
    )
    run = uuid4()
    with pytest.raises(StoreError, match="rejected"):
        await store.append(run, 0, build_log(run, happy_payloads(run)))
    assert _query(db, "SELECT COUNT(*) FROM events") == [(0,)]
    with pytest.raises(RunNotFound):
        await store.read(run)


@pytest.mark.parametrize(
    ("column", "value", "error"),
    [
        ("payload", "'not json'", CorruptEvent),
        ("payload", '\'{"type": "run_created"}\'', CorruptEvent),
        ("event_type", "'run_completed'", CorruptEvent),
        ("schema_version", "7", UnknownEventSchema),
    ],
)
async def test_undecodable_rows_fail_loudly(
    db: Path, column: str, value: str, error: type[Exception]
) -> None:
    store = await SqliteRunStore.open(db)
    run = uuid4()
    await store.append(run, 0, build_log(run, happy_payloads(run)))
    _execute(db, "DROP TRIGGER events_no_update")  # simulate out-of-band corruption
    _execute(db, f"UPDATE events SET {column} = {value} WHERE sequence = 1")  # noqa: S608  # test constants
    with pytest.raises(error):
        await store.read(run)


async def test_database_file_is_private_to_owner(db: Path) -> None:
    await SqliteRunStore.open(db)
    assert db.stat().st_mode & 0o077 == 0


async def test_busy_database_raises_store_unavailable(db: Path) -> None:
    store = await SqliteRunStore.open(db, busy_timeout=0.05)
    run = uuid4()
    with contextlib.closing(sqlite3.connect(db, isolation_level=None)) as locker:
        locker.execute("BEGIN IMMEDIATE")
        with pytest.raises(StoreUnavailable, match="locked"):
            await store.append(run, 0, build_log(run, happy_payloads(run)))
        locker.execute("ROLLBACK")


async def test_uses_default_rollback_journal(db: Path) -> None:
    await SqliteRunStore.open(db)
    assert _query(db, "PRAGMA journal_mode") == [("delete",)]
