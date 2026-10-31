"""ArtifactStore adapters: in-memory and SQLite (ADR-0019).

Both key artifacts by (run id, content-derived artifact id), so a store never returns one run's
artifact to another run, and storing identical content twice is a no-op.
"""

import asyncio
import os
from pathlib import Path
from typing import Self
from uuid import UUID

from hollis.adapters._sqlite import SqliteFile
from hollis.runtime.artifacts import Artifact, ArtifactRef, artifact_ref
from hollis.runtime.tools import ToolTrust

MAX_ARTIFACT_CHARS = 20_000_000
"""Larger results are truncated before storing (with a marker); a guard against runaway tools."""


def _bounded(text: str) -> str:
    if len(text) <= MAX_ARTIFACT_CHARS:
        return text
    return text[:MAX_ARTIFACT_CHARS] + f"\n[artifact truncated at {MAX_ARTIFACT_CHARS} characters]"


class InMemoryArtifactStore:
    def __init__(self) -> None:
        self._artifacts: dict[tuple[UUID, str], Artifact] = {}

    async def put(
        self,
        run_id: UUID,
        text: str,
        *,
        source: str,
        trust: ToolTrust,
        media_type: str = "text/plain",
    ) -> ArtifactRef:
        text = _bounded(text)
        ref = artifact_ref(text, media_type)
        key = (run_id, ref.artifact_id)
        if key not in self._artifacts:
            self._artifacts[key] = Artifact(
                ref=ref, run_id=run_id, source=source, trust=trust, text=text
            )
        return ref

    async def get(self, run_id: UUID, artifact_id: str) -> Artifact | None:
        return self._artifacts.get((run_id, artifact_id))

    async def delete_run(self, run_id: UUID) -> int:
        keys = [key for key in self._artifacts if key[0] == run_id]
        for key in keys:
            del self._artifacts[key]
        return len(keys)


MIGRATIONS = (
    (
        """
        CREATE TABLE artifacts (
            run_id TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            digest TEXT NOT NULL,
            media_type TEXT NOT NULL,
            chars INTEGER NOT NULL,
            source TEXT NOT NULL,
            trust TEXT NOT NULL,
            content TEXT NOT NULL,
            PRIMARY KEY (run_id, artifact_id)
        ) STRICT
        """,
    ),
)


class SqliteArtifactStore:
    def __init__(self, file: SqliteFile) -> None:
        """Use `await SqliteArtifactStore.open(path)`."""
        self._file = file

    @classmethod
    async def open(cls, path: str | os.PathLike[str], *, busy_timeout: float = 5.0) -> Self:
        file = SqliteFile(Path(path), busy_timeout)
        await asyncio.to_thread(file.initialize, MIGRATIONS)
        return cls(file)

    async def put(
        self,
        run_id: UUID,
        text: str,
        *,
        source: str,
        trust: ToolTrust,
        media_type: str = "text/plain",
    ) -> ArtifactRef:
        text = _bounded(text)
        ref = artifact_ref(text, media_type)

        def insert() -> None:
            with self._file.transaction() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(run_id),
                        ref.artifact_id,
                        ref.digest,
                        media_type,
                        ref.chars,
                        source,
                        trust.value,
                        text,
                    ),
                )

        await asyncio.to_thread(insert)
        return ref

    async def get(self, run_id: UUID, artifact_id: str) -> Artifact | None:
        def select() -> tuple[str, str, int, str, str, str] | None:
            with self._file.reading() as conn:
                return conn.execute(
                    "SELECT digest, media_type, chars, source, trust, content FROM artifacts "
                    "WHERE run_id = ? AND artifact_id = ?",
                    (str(run_id), artifact_id),
                ).fetchone()

        row = await asyncio.to_thread(select)
        if row is None:
            return None
        digest, media_type, chars, source, trust, content = row
        return Artifact(
            ref=ArtifactRef(
                artifact_id=artifact_id, chars=chars, digest=digest, media_type=media_type
            ),
            run_id=run_id,
            source=source,
            trust=ToolTrust(trust),
            text=content,
        )

    async def delete_run(self, run_id: UUID) -> int:
        def delete() -> int:
            with self._file.transaction() as conn:
                return conn.execute(
                    "DELETE FROM artifacts WHERE run_id = ?", (str(run_id),)
                ).rowcount

        return await asyncio.to_thread(delete)
