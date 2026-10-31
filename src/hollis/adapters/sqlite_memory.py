"""SQLite MemoryStore with FTS5 keyword search (ADR-0021).

One database file, separate from the run store:
- `memories`: one row per record. Indexed columns for scope, slot, status, validity and dedup;
  the whole record as JSON for exact round-tripping.
- `memories_fts`: an FTS5 index (Porter stemming, Unicode tokenizer) over content, subject, key
  and tags, kept in sync on every save and delete. Ranking uses FTS5's built-in BM25.
- `memory_changes`: the append-only change log (triggers refuse updates and deletes). Deleting a
  memory leaves a tombstone row without content.
- `memory_jobs`: idempotency markers for multi-step maintenance.

Every query filters by scope in SQL. Timestamps are stored as fixed-width UTC strings so they
compare correctly as text. `secure_delete` is on, so deleted text is overwritten in the file.
"""

import asyncio
import os
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from hollis.adapters._sqlite import SqliteFile
from hollis.memory.model import ChangeAction, MemoryChange, MemoryRecord, MemoryStatus
from hollis.runtime.memory import MemoryKind
from hollis.runtime.text import query_terms

MIGRATIONS = (
    (
        """
        CREATE TABLE memories (
            memory_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            kind TEXT NOT NULL,
            subject TEXT,
            key TEXT,
            status TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            valid_from TEXT,
            valid_until TEXT,
            content_hash TEXT NOT NULL,
            dedup_key TEXT,
            data TEXT NOT NULL
        ) STRICT
        """,
        "CREATE INDEX memories_scope ON memories (scope, status)",
        "CREATE INDEX memories_slot ON memories (scope, subject, key)",
        "CREATE INDEX memories_hash ON memories (scope, content_hash)",
        """CREATE UNIQUE INDEX memories_dedup ON memories (scope, dedup_key)
           WHERE dedup_key IS NOT NULL""",
        """
        CREATE VIRTUAL TABLE memories_fts USING fts5(
            memory_id UNINDEXED, content, subject, tags, tokenize = 'porter unicode61'
        )
        """,
        """
        CREATE TABLE memory_changes (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT NOT NULL,
            scope TEXT NOT NULL,
            at TEXT NOT NULL,
            action TEXT NOT NULL,
            actor TEXT NOT NULL,
            detail TEXT NOT NULL
        ) STRICT
        """,
        "CREATE INDEX memory_changes_id ON memory_changes (memory_id)",
        """
        CREATE TRIGGER memory_changes_no_update BEFORE UPDATE ON memory_changes
        BEGIN SELECT RAISE(ABORT, 'the memory change log is append-only'); END
        """,
        """
        CREATE TRIGGER memory_changes_no_delete BEFORE DELETE ON memory_changes
        BEGIN SELECT RAISE(ABORT, 'the memory change log is append-only'); END
        """,
        "CREATE TABLE memory_jobs (key TEXT PRIMARY KEY, at TEXT NOT NULL) STRICT",
    ),
)

_VALID_AT = """
    m.status != 'retracted' AND m.recorded_at <= :at
    AND (m.valid_from IS NULL OR m.valid_from <= :at)
    AND (m.valid_until IS NULL OR m.valid_until > :at)
"""


def _ts(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _in(name: str, values: Sequence[str]) -> tuple[str, dict[str, str]]:
    """`name IN (:name0, :name1, ...)` with bound parameters (never interpolated values)."""
    params = {f"{name.replace('.', '_')}{i}": v for i, v in enumerate(values)}
    return f"{name} IN ({', '.join(':' + k for k in params)})", params


class SqliteMemoryStore:
    def __init__(self, file: SqliteFile) -> None:
        """Use `await SqliteMemoryStore.open(path)`."""
        self._file = file

    @classmethod
    async def open(cls, path: str | os.PathLike[str], *, busy_timeout: float = 5.0) -> Self:
        file = SqliteFile(Path(path), busy_timeout)
        await asyncio.to_thread(file.initialize, MIGRATIONS)
        return cls(file)

    async def save(self, records: Sequence[MemoryRecord], changes: Sequence[MemoryChange]) -> None:
        def write() -> None:
            with self._file.transaction() as conn:
                for record in records:
                    _upsert(conn, record)
                _log(conn, changes)

        await asyncio.to_thread(write)

    async def get(self, memory_id: str) -> MemoryRecord | None:
        rows = await self._query(
            "SELECT data FROM memories m WHERE memory_id = :id", {"id": memory_id}
        )
        return rows[0] if rows else None

    async def find(
        self,
        scope: str,
        *,
        subject: str | None = None,
        key: str | None = None,
        content_hash: str | None = None,
        dedup_key: str | None = None,
        statuses: Sequence[MemoryStatus] = (MemoryStatus.ACTIVE,),
    ) -> list[MemoryRecord]:
        where = ["m.scope = :scope"]
        params: dict[str, Any] = {"scope": scope}
        for column, value in (
            ("subject", subject),
            ("key", key),
            ("content_hash", content_hash),
            ("dedup_key", dedup_key),
        ):
            if value is not None:
                where.append(f"m.{column} = :{column}")
                params[column] = value
        if statuses:
            clause, extra = _in("m.status", [s.value for s in statuses])
            where.append(clause)
            params |= extra
        sql = f"SELECT data FROM memories m WHERE {' AND '.join(where)} ORDER BY m.recorded_at"  # noqa: S608  # fixed columns, bound values
        return await self._query(sql, params)

    async def search(
        self,
        scopes: Sequence[str],
        query: Sequence[str],
        *,
        as_of: datetime,
        kinds: Sequence[MemoryKind] = (),
        limit: int = 50,
    ) -> list[tuple[MemoryRecord, float]]:
        words = [w for q in query for w in query_terms(q)]
        if not scopes or not words:
            return []
        match = " OR ".join('"' + w.replace('"', "") + '"' for w in dict.fromkeys(words))
        scope_clause, params = _in("m.scope", scopes)
        params |= {"match": match, "at": _ts(as_of), "limit": limit}
        kind_clause = ""
        if kinds:
            clause, extra = _in("m.kind", [k.value for k in kinds])
            kind_clause = f"AND {clause}"
            params |= extra
        sql = f"""
            SELECT m.data, -bm25(memories_fts) AS score
            FROM memories_fts JOIN memories m ON m.memory_id = memories_fts.memory_id
            WHERE memories_fts MATCH :match AND {scope_clause} AND {_VALID_AT} {kind_clause}
            ORDER BY score DESC, m.memory_id LIMIT :limit
        """  # noqa: S608  # fixed clauses, bound values

        def select() -> list[tuple[str, float]]:
            with self._file.reading() as conn:
                return conn.execute(sql, params).fetchall()

        rows = await asyncio.to_thread(select)
        return [(MemoryRecord.model_validate_json(data), max(score, 1e-9)) for data, score in rows]

    async def valid(
        self,
        scopes: Sequence[str],
        *,
        as_of: datetime,
        kinds: Sequence[MemoryKind] = (),
        subjects: Sequence[str] = (),
        limit: int = 100,
    ) -> list[MemoryRecord]:
        if not scopes:
            return []
        scope_clause, params = _in("m.scope", scopes)
        params |= {"at": _ts(as_of), "limit": limit}
        extra_clauses = ""
        if kinds:
            clause, extra = _in("m.kind", [k.value for k in kinds])
            extra_clauses += f" AND {clause}"
            params |= extra
        if subjects:
            clause, extra = _in("m.subject", list(subjects))
            extra_clauses += f" AND {clause}"
            params |= extra
        sql = f"""SELECT data FROM memories m WHERE {scope_clause} AND {_VALID_AT} {extra_clauses}
                  ORDER BY m.recorded_at DESC, m.memory_id DESC LIMIT :limit"""  # noqa: S608  # fixed clauses, bound values
        return await self._query(sql, params)

    async def records(self, scope: str | None = None) -> list[MemoryRecord]:
        if scope is None:
            return await self._query("SELECT data FROM memories m ORDER BY recorded_at", {})
        return await self._query(
            "SELECT data FROM memories m WHERE scope = :scope ORDER BY recorded_at",
            {"scope": scope},
        )

    async def subjects(self, scopes: Sequence[str]) -> list[str]:
        if not scopes:
            return []
        clause, params = _in("scope", scopes)
        sql = f"""SELECT DISTINCT subject FROM memories WHERE {clause}
                  AND status = 'active' AND subject IS NOT NULL ORDER BY subject"""  # noqa: S608  # bound values

        def select() -> list[tuple[str]]:
            with self._file.reading() as conn:
                return conn.execute(sql, params).fetchall()

        return [row[0] for row in await asyncio.to_thread(select)]

    async def delete(self, memory_ids: Sequence[str], changes: Sequence[MemoryChange]) -> int:
        def remove() -> int:
            with self._file.transaction() as conn:
                deleted = 0
                for memory_id in memory_ids:
                    conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (memory_id,))
                    deleted += conn.execute(
                        "DELETE FROM memories WHERE memory_id = ?", (memory_id,)
                    ).rowcount
                _log(conn, changes)
                return deleted

        return await asyncio.to_thread(remove)

    async def changes(self, memory_id: str) -> list[MemoryChange]:
        def select() -> list[tuple[str, str, str, str, str, str]]:
            with self._file.reading() as conn:
                return conn.execute(
                    "SELECT memory_id, scope, at, action, actor, detail FROM memory_changes "
                    "WHERE memory_id = ? ORDER BY seq",
                    (memory_id,),
                ).fetchall()

        return [
            MemoryChange(
                memory_id=row[0],
                scope=row[1],
                at=datetime.strptime(row[2], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC),
                action=ChangeAction(row[3]),
                actor=row[4],
                detail=row[5],
            )
            for row in await asyncio.to_thread(select)
        ]

    async def count(self, scope: str) -> int:
        def select() -> int:
            with self._file.reading() as conn:
                return int(
                    conn.execute(
                        "SELECT COUNT(*) FROM memories WHERE scope = ?", (scope,)
                    ).fetchone()[0]
                )

        return await asyncio.to_thread(select)

    async def mark_recalled(self, memory_ids: Sequence[str], at: datetime) -> None:
        def update() -> None:
            with self._file.transaction() as conn:
                for memory_id in memory_ids:
                    row = conn.execute(
                        "SELECT data FROM memories WHERE memory_id = ?", (memory_id,)
                    ).fetchone()
                    if row is None:
                        continue
                    record = MemoryRecord.model_validate_json(row[0])
                    record = record.model_copy(
                        update={"recall_count": record.recall_count + 1, "last_recalled_at": at}
                    )
                    conn.execute(
                        "UPDATE memories SET data = ? WHERE memory_id = ?",
                        (record.model_dump_json(), memory_id),
                    )

        await asyncio.to_thread(update)

    async def job_done(self, key: str) -> bool:
        def select() -> bool:
            with self._file.reading() as conn:
                return (
                    conn.execute("SELECT 1 FROM memory_jobs WHERE key = ?", (key,)).fetchone()
                    is not None
                )

        return await asyncio.to_thread(select)

    async def mark_job(self, key: str, at: datetime) -> None:
        def insert() -> None:
            with self._file.transaction() as conn:
                conn.execute("INSERT OR IGNORE INTO memory_jobs VALUES (?, ?)", (key, _ts(at)))

        await asyncio.to_thread(insert)

    async def _query(self, sql: str, params: dict[str, Any]) -> list[MemoryRecord]:
        def select() -> list[tuple[str]]:
            with self._file.reading() as conn:
                return conn.execute(sql, params).fetchall()

        return [MemoryRecord.model_validate_json(row[0]) for row in await asyncio.to_thread(select)]


def _upsert(conn: sqlite3.Connection, record: MemoryRecord) -> None:
    try:
        conn.execute(
            """INSERT INTO memories VALUES (:id, :scope, :kind, :subject, :key, :status,
                   :recorded_at, :valid_from, :valid_until, :hash, :dedup, :data)
               ON CONFLICT (memory_id) DO UPDATE SET
                   status = excluded.status, valid_from = excluded.valid_from,
                   valid_until = excluded.valid_until, data = excluded.data""",
            {
                "id": record.memory_id,
                "scope": record.scope,
                "kind": record.kind.value,
                "subject": record.subject,
                "key": record.key,
                "status": record.status.value,
                "recorded_at": _ts(record.recorded_at),
                "valid_from": _ts(record.valid_from),
                "valid_until": _ts(record.valid_until),
                "hash": record.content_hash,
                "dedup": record.dedup_key,
                "data": record.model_dump_json(),
            },
        )
    except sqlite3.IntegrityError as exc:
        raise ValueError(f"memory {record.memory_id} rejected by the store: {exc}") from exc
    conn.execute("DELETE FROM memories_fts WHERE memory_id = ?", (record.memory_id,))
    conn.execute(
        "INSERT INTO memories_fts (memory_id, content, subject, tags) VALUES (?, ?, ?, ?)",
        (
            record.memory_id,
            record.content,
            " ".join(filter(None, (record.subject, record.key))),
            " ".join(record.tags),
        ),
    )


def _log(conn: sqlite3.Connection, changes: Sequence[MemoryChange]) -> None:
    conn.executemany(
        "INSERT INTO memory_changes (memory_id, scope, at, action, actor, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(c.memory_id, c.scope, _ts(c.at), c.action.value, c.actor, c.detail) for c in changes],
    )
