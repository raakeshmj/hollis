"""The workspace database (`.hollis/workspace.db`): principals, tokens, sessions, agents, the run
index and an audit log.

The run index pairs ownership recorded when a run starts (agent, created_by, session: workspace
data, never rewritten) with columns derived from the run's event log (status, usage, ...), which
`Workspace.sync_index` can re-derive at any time; the logs in `runs.db` remain the source of truth
for everything a run did. A run whose ownership was never recorded (the process stopped between
creating it and indexing it) is visible to admins only.

Tokens and sessions are stored only as SHA-256 hashes of high-entropy random secrets. The audit
log is append-only.
"""

import asyncio
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self
from uuid import UUID

from hollis.adapters._sqlite import SqliteFile

MIGRATIONS = (
    (
        """CREATE TABLE principals (
            name TEXT PRIMARY KEY, role TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL
        ) STRICT""",
        """CREATE TABLE tokens (
            token_id TEXT PRIMARY KEY, principal TEXT NOT NULL REFERENCES principals (name),
            hash TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, label TEXT NOT NULL,
            created_at TEXT NOT NULL, last_used_at TEXT, expires_at TEXT, revoked_at TEXT
        ) STRICT""",
        """CREATE TABLE sessions (
            hash TEXT PRIMARY KEY, principal TEXT NOT NULL REFERENCES principals (name),
            created_at TEXT NOT NULL, expires_at TEXT NOT NULL
        ) STRICT""",
        """CREATE TABLE agents (
            name TEXT PRIMARY KEY, data TEXT NOT NULL, version INTEGER NOT NULL
        ) STRICT""",
        """CREATE TABLE run_index (
            run_id TEXT PRIMARY KEY, agent TEXT, created_by TEXT, session TEXT,
            status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            version INTEGER NOT NULL, awaiting INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL
        ) STRICT""",
        "CREATE INDEX run_index_created ON run_index (created_at DESC)",
        "CREATE INDEX run_index_owner ON run_index (created_by, created_at DESC)",
        """CREATE TABLE audit (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, principal TEXT NOT NULL,
            action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL
        ) STRICT""",
        """CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
           BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END""",
        """CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
           BEGIN SELECT RAISE(ABORT, 'the audit log is append-only'); END""",
    ),
    (  # M7: run lineage, derived from RunCreated like the other index columns
        "ALTER TABLE run_index ADD COLUMN parent_id TEXT",
        "ALTER TABLE run_index ADD COLUMN root_id TEXT",
        "ALTER TABLE run_index ADD COLUMN depth INTEGER NOT NULL DEFAULT 0",
        "CREATE INDEX run_index_parent ON run_index (parent_id)",
        "CREATE INDEX run_index_root ON run_index (root_id, created_at)",
    ),
)

_COLUMNS = (
    "run_id, agent, created_by, session, status, created_at, updated_at, version, awaiting, data, "
    "parent_id, root_id, depth, "
    "(SELECT COUNT(*) FROM run_index AS c WHERE c.parent_id = run_index.run_id) AS children"
)


def ts(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def parse_ts(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


class WorkspaceDb:
    def __init__(self, file: SqliteFile) -> None:
        self._file = file

    @classmethod
    async def open(cls, path: str | os.PathLike[str]) -> Self:
        file = SqliteFile(Path(path), 5.0)
        await asyncio.to_thread(file.initialize, MIGRATIONS)
        return cls(file)

    @property
    def path(self) -> Path:
        return self._file.path

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        def run() -> int:
            with self._file.transaction() as conn:
                return conn.execute(sql, params).rowcount

        return await asyncio.to_thread(run)

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        def run() -> list[tuple[Any, ...]]:
            with self._file.reading() as conn:
                return conn.execute(sql, params).fetchall()

        return await asyncio.to_thread(run)

    async def audit(self, principal: str, action: str, target: str, detail: str = "") -> None:
        await self.execute(
            "INSERT INTO audit (at, principal, action, target, detail) VALUES (?, ?, ?, ?, ?)",
            (ts(datetime.now(UTC)), principal, action, target, detail[:1000]),
        )

    async def audit_log(self, limit: int = 100) -> list[dict[str, str]]:
        rows = await self.query(
            "SELECT at, principal, action, target, detail FROM audit ORDER BY seq DESC LIMIT ?",
            (limit,),
        )
        return [
            {"at": at, "principal": p, "action": a, "target": t, "detail": d}
            for at, p, a, t, d in rows
        ]

    # --- run index ------------------------------------------------------------------------------

    async def index_upsert(
        self,
        run_id: UUID,
        *,
        status: str,
        created_at: datetime,
        updated_at: datetime,
        version: int,
        awaiting: int,
        data: dict[str, Any],
        parent_id: UUID | None = None,
        root_id: UUID | None = None,
        depth: int = 0,
    ) -> None:
        await self.execute(
            """INSERT INTO run_index
                   (run_id, status, created_at, updated_at, version, awaiting, data,
                    parent_id, root_id, depth)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (run_id) DO UPDATE SET status = excluded.status,
                   updated_at = excluded.updated_at, version = excluded.version,
                   awaiting = excluded.awaiting, data = json_patch(run_index.data, excluded.data),
                   parent_id = excluded.parent_id, root_id = excluded.root_id,
                   depth = excluded.depth
               WHERE excluded.version >= run_index.version""",
            (
                str(run_id),
                status,
                ts(created_at),
                ts(updated_at),
                version,
                awaiting,
                json.dumps(data),
                str(parent_id) if parent_id else None,
                str(root_id) if root_id else None,
                depth,
            ),
        )

    async def index_set_owner(
        self, run_id: UUID, *, agent: str | None, created_by: str, session: str | None
    ) -> None:
        await self.execute(
            "UPDATE run_index SET agent = ?, created_by = ?, session = ? WHERE run_id = ?",
            (agent, created_by, session, str(run_id)),
        )

    async def index_patch(self, run_id: UUID, data: dict[str, Any]) -> None:
        await self.execute(
            "UPDATE run_index SET data = json_patch(data, ?) WHERE run_id = ?",
            (json.dumps(data), str(run_id)),
        )

    async def index_versions(self) -> dict[UUID, int]:
        rows = await self.query("SELECT run_id, version FROM run_index")
        return {UUID(run_id): version for run_id, version in rows}

    async def index_get(self, run_id: UUID) -> dict[str, Any] | None:
        sql = f"SELECT {_COLUMNS} FROM run_index WHERE run_id = ?"  # noqa: S608  # fixed fragments
        rows = await self.query(sql, (str(run_id),))
        return _row(rows[0]) if rows else None

    async def index_tree(self, root_id: UUID) -> list[dict[str, Any]]:
        """A root run and every run delegated below it, oldest first."""
        rows = await self.query(
            f"SELECT {_COLUMNS} FROM run_index WHERE run_id = ? OR root_id = ? "  # noqa: S608  # fixed fragments
            "ORDER BY created_at, run_id",
            (str(root_id), str(root_id)),
        )
        return [_row(r) for r in rows]

    async def index_children(self, parent_id: UUID) -> list[dict[str, Any]]:
        rows = await self.query(
            f"SELECT {_COLUMNS} FROM run_index WHERE parent_id = ? ORDER BY created_at, run_id",  # noqa: S608  # fixed fragments
            (str(parent_id),),
        )
        return [_row(r) for r in rows]

    async def index_list(
        self,
        *,
        owner: str | None,
        status: str | None = None,
        agent: str | None = None,
        roots: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where: list[str] = []
        params: list[Any] = []
        if roots:
            where.append("parent_id IS NULL")
        if owner is not None:
            where.append("created_by = ?")
            params.append(owner)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        if agent is not None:
            where.append("agent = ?")
            params.append(agent)
        # The clause is assembled only from the fixed fragments above; values are parameters.
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        count_sql = f"SELECT COUNT(*) FROM run_index {clause}"  # noqa: S608  # fixed fragments
        total = (await self.query(count_sql, params))[0][0]
        list_sql = (
            f"SELECT {_COLUMNS} FROM run_index {clause} "  # noqa: S608  # fixed fragments
            "ORDER BY created_at DESC LIMIT ? OFFSET ?"
        )
        rows = await self.query(list_sql, [*params, limit, offset])
        return [_row(r) for r in rows], int(total)


def _row(row: tuple[Any, ...]) -> dict[str, Any]:
    run_id, agent, created_by, session, status, created, updated, version, awaiting, data = row[:10]
    parent_id, root_id, depth, children = row[10:]
    return {
        "parent_id": UUID(parent_id) if parent_id else None,
        "root_id": UUID(root_id) if root_id else None,
        "depth": depth,
        "children": children,
        "run_id": UUID(run_id),
        "agent": agent,
        "created_by": created_by,
        "session": session,
        "status": status,
        "created_at": parse_ts(created),
        "updated_at": parse_ts(updated),
        "version": version,
        "awaiting": awaiting,
        "data": json.loads(data),
    }
