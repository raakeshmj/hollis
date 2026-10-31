"""SQLite KnowledgeStore: FTS5, identifier index and per-space vectors (ADR-0024).

One database file, separate from runs, artifacts and memory. Tables:
- `collections`, `sources` (+ `source_labels` for access control), `sections`, `chunks`;
- `chunks_fts`: FTS5 (Porter, Unicode) over chunk text plus identifier parts and the section
  breadcrumb, for BM25;
- `chunk_identifiers`: identifiers indexed whole (lower-cased) for exact lookup;
- `vectors`: float32 vectors keyed by (chunk, embedding space); dense search is an exact scan
  over the visible chunks of one space (no approximate index; see ADR-0024 for measured scale);
- `knowledge_changes`: append-only, content-free change log.

Access control is part of every query's SQL (`_visible`), so restricted chunks are never scored,
ranked or returned. `secure_delete` is on.
"""

import asyncio
import os
import sqlite3
from array import array
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from hollis.adapters._sqlite import SqliteFile
from hollis.knowledge.model import Chunk, Collection, Section, Source
from hollis.knowledge.store import ReconcileReport, StoreStats
from hollis.runtime.embedding import EmbeddingMismatch, EmbeddingSpace
from hollis.runtime.knowledge import KnowledgeAccess
from hollis.runtime.text import analyze, identifiers, parts, tokens

MIGRATIONS = (
    (
        """CREATE TABLE collections (
            name TEXT PRIMARY KEY, description TEXT NOT NULL, space TEXT, created_at TEXT NOT NULL
        ) STRICT""",
        """CREATE TABLE sources (
            source_id TEXT PRIMARY KEY,
            collection TEXT NOT NULL REFERENCES collections (name),
            key TEXT NOT NULL,
            data TEXT NOT NULL,
            UNIQUE (collection, key)
        ) STRICT""",
        """CREATE TABLE source_labels (
            source_id TEXT NOT NULL REFERENCES sources (source_id), label TEXT NOT NULL,
            PRIMARY KEY (source_id, label)
        ) STRICT""",
        """CREATE TABLE sections (
            section_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, idx INTEGER NOT NULL,
            data TEXT NOT NULL
        ) STRICT""",
        "CREATE INDEX sections_source ON sections (source_id, idx)",
        """CREATE TABLE chunks (
            chunk_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL REFERENCES sources (source_id),
            collection TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            data TEXT NOT NULL
        ) STRICT""",
        "CREATE INDEX chunks_source ON chunks (source_id, ordinal)",
        """CREATE VIRTUAL TABLE chunks_fts USING fts5(
            chunk_id UNINDEXED, body, path, tokenize = 'porter unicode61'
        )""",
        """CREATE TABLE chunk_identifiers (
            term TEXT NOT NULL, chunk_id TEXT NOT NULL, PRIMARY KEY (term, chunk_id)
        ) STRICT, WITHOUT ROWID""",
        """CREATE TABLE vectors (
            chunk_id TEXT NOT NULL, space TEXT NOT NULL, vector BLOB NOT NULL,
            PRIMARY KEY (chunk_id, space)
        ) STRICT""",
        """CREATE TABLE knowledge_changes (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, action TEXT NOT NULL,
            collection TEXT NOT NULL, source_key TEXT NOT NULL, actor TEXT NOT NULL,
            detail TEXT NOT NULL
        ) STRICT""",
        """CREATE TRIGGER knowledge_changes_no_update BEFORE UPDATE ON knowledge_changes
           BEGIN SELECT RAISE(ABORT, 'the knowledge change log is append-only'); END""",
        """CREATE TRIGGER knowledge_changes_no_delete BEFORE DELETE ON knowledge_changes
           BEGIN SELECT RAISE(ABORT, 'the knowledge change log is append-only'); END""",
    ),
)

_CHUNK_COLUMNS = "c.data"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _in(name: str, prefix: str, values: Sequence[str]) -> tuple[str, dict[str, str]]:
    params = {f"{prefix}{i}": v for i, v in enumerate(values)}
    return f"{name} IN ({', '.join(':' + k for k in params)})", params


def _visible(access: KnowledgeAccess, source: str = "s") -> tuple[str, dict[str, str]] | None:
    """SQL restricting `source` rows to the grant, or None when the grant allows nothing."""
    if not access.collections:
        return None
    collections, params = _in(f"{source}.collection", "vc", access.collections)
    unlabelled = (
        "NOT EXISTS (SELECT 1 FROM source_labels vl "  # noqa: S608  # fixed alias, no values
        f"WHERE vl.source_id = {source}.source_id)"
    )
    if not access.labels:
        return f"{collections} AND {unlabelled}", params
    labels, label_params = _in("vl2.label", "vlab", access.labels)
    labelled = (
        "EXISTS (SELECT 1 FROM source_labels vl2 "  # noqa: S608  # fixed alias, bound values
        f"WHERE vl2.source_id = {source}.source_id AND {labels})"
    )
    return f"{collections} AND ({unlabelled} OR {labelled})", params | label_params


def _pack(vector: Sequence[float]) -> bytes:
    return array("f", vector).tobytes()


def _unpack(blob: bytes) -> array[float]:
    values = array("f")
    values.frombytes(blob)
    return values


def _chunk(data: str) -> Chunk:
    return Chunk.model_validate_json(data)


def fts_body(chunk: Chunk) -> str:
    """Indexed text: the chunk plus identifier parts (camelCase is not split by FTS5)."""
    extra = [p for token in tokens(chunk.text) for p in parts(token)]
    return chunk.text + ("\n" + " ".join(extra) if extra else "")


class SqliteKnowledgeStore:
    def __init__(self, file: SqliteFile) -> None:
        """Use `await SqliteKnowledgeStore.open(path)`."""
        self._file = file

    @classmethod
    async def open(cls, path: str | os.PathLike[str], *, busy_timeout: float = 5.0) -> Self:
        file = SqliteFile(Path(path), busy_timeout)
        await asyncio.to_thread(file.initialize, MIGRATIONS)
        return cls(file)

    # --- collections ----------------------------------------------------------------------------

    async def create_collection(self, collection: Collection, actor: str) -> None:
        def write() -> None:
            with self._file.transaction() as conn:
                try:
                    conn.execute(
                        "INSERT INTO collections VALUES (?, ?, ?, ?)",
                        (
                            collection.name,
                            collection.description,
                            collection.space.model_dump_json() if collection.space else None,
                            collection.created_at.isoformat(),
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ValueError(f"collection {collection.name!r} already exists") from exc
                _log(conn, "collection_created", collection.name, "", actor, "")

        await asyncio.to_thread(write)

    async def collection(self, name: str) -> Collection | None:
        rows = await self._rows(
            "SELECT name, description, space, created_at FROM collections WHERE name = ?", (name,)
        )
        return _collection(rows[0]) if rows else None

    async def collections(self) -> list[Collection]:
        rows = await self._rows(
            "SELECT name, description, space, created_at FROM collections ORDER BY name", ()
        )
        return [_collection(r) for r in rows]

    async def delete_collection(self, name: str, actor: str, reason: str) -> int:
        def delete() -> int:
            with self._file.transaction() as conn:
                ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT source_id FROM sources WHERE collection = ?", (name,)
                    )
                ]
                for source_id in ids:
                    _delete_source_rows(conn, source_id)
                deleted = conn.execute("DELETE FROM collections WHERE name = ?", (name,)).rowcount
                if deleted:
                    _log(
                        conn, "collection_deleted", name, "", actor, f"{len(ids)} sources; {reason}"
                    )
                return len(ids) if deleted else -1

        return await asyncio.to_thread(delete)

    # --- sources --------------------------------------------------------------------------------

    async def source(self, collection: str, key: str) -> Source | None:
        rows = await self._rows(
            "SELECT data FROM sources WHERE collection = ? AND key = ?", (collection, key)
        )
        return Source.model_validate_json(rows[0][0]) if rows else None

    async def sources(self, collection: str) -> list[Source]:
        rows = await self._rows(
            "SELECT data FROM sources WHERE collection = ? ORDER BY key", (collection,)
        )
        return [Source.model_validate_json(r[0]) for r in rows]

    async def replace_source(
        self,
        source: Source,
        sections: Sequence[Section],
        chunks: Sequence[Chunk],
        vectors: Mapping[str, Sequence[float]],
        actor: str,
    ) -> None:
        def write() -> None:
            with self._file.transaction() as conn:
                row = conn.execute(
                    "SELECT space FROM collections WHERE name = ?", (source.collection,)
                ).fetchone()
                if row is None:
                    raise LookupError(f"no collection {source.collection!r}")
                space = EmbeddingSpace.model_validate_json(row[0]) if row[0] else None
                if space is not None:
                    missing = [c.chunk_id for c in chunks if c.chunk_id not in vectors]
                    if missing:
                        raise EmbeddingMismatch(
                            f"{len(missing)} chunks have no vector in {space.key}"
                        )
                    wrong = [cid for cid, v in vectors.items() if len(v) != space.dimensions]
                    if wrong:
                        raise EmbeddingMismatch(
                            f"{len(wrong)} vectors do not have {space.dimensions} dimensions"
                        )
                existed = conn.execute(
                    "SELECT 1 FROM sources WHERE source_id = ?", (source.source_id,)
                ).fetchone()
                _delete_source_rows(conn, source.source_id)
                conn.execute(
                    "INSERT INTO sources VALUES (?, ?, ?, ?)",
                    (source.source_id, source.collection, source.key, source.model_dump_json()),
                )
                conn.executemany(
                    "INSERT INTO source_labels VALUES (?, ?)",
                    [(source.source_id, label) for label in source.labels],
                )
                conn.executemany(
                    "INSERT INTO sections VALUES (?, ?, ?, ?)",
                    [(s.section_id, s.source_id, s.index, s.model_dump_json()) for s in sections],
                )
                for chunk in chunks:
                    conn.execute(
                        "INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
                        (
                            chunk.chunk_id,
                            chunk.source_id,
                            chunk.collection,
                            chunk.ordinal,
                            chunk.model_dump_json(),
                        ),
                    )
                    conn.execute(
                        "INSERT INTO chunks_fts (chunk_id, body, path) VALUES (?, ?, ?)",
                        (chunk.chunk_id, fts_body(chunk), " ".join([source.title, *chunk.path])),
                    )
                    conn.executemany(
                        "INSERT OR IGNORE INTO chunk_identifiers VALUES (?, ?)",
                        [(i.casefold(), chunk.chunk_id) for i in identifiers(chunk.text)],
                    )
                    if space is not None:
                        conn.execute(
                            "INSERT INTO vectors VALUES (?, ?, ?)",
                            (chunk.chunk_id, space.key, _pack(vectors[chunk.chunk_id])),
                        )
                action = "source_updated" if existed else "source_created"
                _log(
                    conn,
                    action,
                    source.collection,
                    source.key,
                    actor,
                    f"version {source.version}, {len(chunks)} chunks",
                )

        await asyncio.to_thread(write)

    async def delete_source(self, source_id: str, actor: str, reason: str) -> bool:
        def delete() -> bool:
            with self._file.transaction() as conn:
                row = conn.execute(
                    "SELECT collection, key FROM sources WHERE source_id = ?", (source_id,)
                ).fetchone()
                if row is None:
                    return False
                _delete_source_rows(conn, source_id)
                _log(conn, "source_deleted", row[0], row[1], actor, reason)
                return True

        return await asyncio.to_thread(delete)

    async def set_labels(self, source_id: str, labels: Sequence[str], actor: str) -> None:
        def write() -> None:
            with self._file.transaction() as conn:
                row = conn.execute(
                    "SELECT data FROM sources WHERE source_id = ?", (source_id,)
                ).fetchone()
                if row is None:
                    raise LookupError(f"no source {source_id!r}")
                source = Source.model_validate_json(row[0]).model_copy(
                    update={"labels": tuple(labels)}
                )
                conn.execute(
                    "UPDATE sources SET data = ? WHERE source_id = ?",
                    (source.model_dump_json(), source_id),
                )
                conn.execute("DELETE FROM source_labels WHERE source_id = ?", (source_id,))
                conn.executemany(
                    "INSERT INTO source_labels VALUES (?, ?)",
                    [(source_id, label) for label in labels],
                )
                _log(
                    conn,
                    "labels_changed",
                    source.collection,
                    source.key,
                    actor,
                    ",".join(labels) or "(none)",
                )

        await asyncio.to_thread(write)

    # --- retrieval ------------------------------------------------------------------------------

    async def search_lexical(
        self, access: KnowledgeAccess, terms: Sequence[str], *, limit: int
    ) -> list[tuple[Chunk, float]]:
        visible = _visible(access)
        words = [t for term in terms for t in analyze(term)]
        if visible is None or not words:
            return []
        clause, params = visible
        match = " OR ".join('"' + w.replace('"', "") + '"' for w in dict.fromkeys(words))
        sql = f"""
            SELECT c.data, -bm25(chunks_fts, 1.0, 0.5) AS score
            FROM chunks_fts
            JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
            JOIN sources s ON s.source_id = c.source_id
            WHERE chunks_fts MATCH :match AND {clause}
            ORDER BY score DESC, c.chunk_id LIMIT :limit
        """  # noqa: S608  # fixed clauses, bound values
        rows = await self._rows(sql, {**params, "match": match, "limit": limit})
        return [(_chunk(data), max(float(score), 1e-9)) for data, score in rows]

    async def search_identifiers(
        self, access: KnowledgeAccess, identifiers: Sequence[str], *, limit: int
    ) -> list[tuple[Chunk, int]]:
        visible = _visible(access)
        terms = list(dict.fromkeys(i.casefold() for i in identifiers))
        if visible is None or not terms:
            return []
        clause, params = visible
        term_clause, term_params = _in("ci.term", "term", terms)
        sql = f"""
            SELECT c.data, COUNT(DISTINCT ci.term) AS matched
            FROM chunk_identifiers ci
            JOIN chunks c ON c.chunk_id = ci.chunk_id
            JOIN sources s ON s.source_id = c.source_id
            WHERE {term_clause} AND {clause}
            GROUP BY c.chunk_id ORDER BY matched DESC, c.chunk_id LIMIT :limit
        """  # noqa: S608  # fixed clauses, bound values
        rows = await self._rows(sql, {**params, **term_params, "limit": limit})
        return [(_chunk(data), int(matched)) for data, matched in rows]

    async def search_dense(
        self, access: KnowledgeAccess, space: EmbeddingSpace, vector: Sequence[float], *, limit: int
    ) -> list[tuple[Chunk, float]]:
        visible = _visible(access)
        if visible is None:
            return []
        if len(vector) != space.dimensions:
            raise EmbeddingMismatch(
                f"query vector has {len(vector)} dimensions; {space.key} has {space.dimensions}"
            )
        clause, params = visible
        sql = f"""
            SELECT c.chunk_id, v.vector FROM vectors v
            JOIN chunks c ON c.chunk_id = v.chunk_id
            JOIN sources s ON s.source_id = c.source_id
            JOIN collections k ON k.name = s.collection
            WHERE v.space = :space AND json_extract(k.space, '$.dimensions') = :dims AND {clause}
        """  # noqa: S608  # fixed clauses, bound values
        rows = await self._rows(sql, {**params, "space": space.key, "dims": space.dimensions})
        query = list(vector)
        scored = sorted(
            (
                (sum(a * b for a, b in zip(query, _unpack(blob), strict=True)), chunk_id)
                for chunk_id, blob in rows
            ),
            key=lambda pair: (-pair[0], pair[1]),
        )[:limit]
        if not scored:
            return []
        chunks = {c.chunk_id: c for c in await self._chunks_by_id([cid for _, cid in scored])}
        return [(chunks[cid], score) for score, cid in scored if cid in chunks]

    async def chunk(self, access: KnowledgeAccess, chunk_id: str) -> Chunk | None:
        found = await self._visible_chunks(access, "c.chunk_id = :id", {"id": chunk_id})
        return found[0] if found else None

    async def visible_source(self, access: KnowledgeAccess, source_id: str) -> Source | None:
        visible = _visible(access)
        if visible is None:
            return None
        clause, params = visible
        rows = await self._rows(
            f"SELECT s.data FROM sources s WHERE s.source_id = :id AND {clause}",  # noqa: S608  # bound values
            {**params, "id": source_id},
        )
        return Source.model_validate_json(rows[0][0]) if rows else None

    async def source_chunks(self, access: KnowledgeAccess, source_id: str) -> list[Chunk]:
        return await self._visible_chunks(access, "c.source_id = :id", {"id": source_id})

    async def source_sections(self, access: KnowledgeAccess, source_id: str) -> list[Section]:
        if await self.visible_source(access, source_id) is None:
            return []
        rows = await self._rows(
            "SELECT data FROM sections WHERE source_id = ? ORDER BY idx", (source_id,)
        )
        return [Section.model_validate_json(r[0]) for r in rows]

    async def chunks_for_embedding(self, collection: str) -> list[Chunk]:
        rows = await self._rows(
            "SELECT data FROM chunks WHERE collection = ? ORDER BY source_id, ordinal",
            (collection,),
        )
        return [_chunk(r[0]) for r in rows]

    async def set_space(
        self,
        collection: str,
        space: EmbeddingSpace | None,
        vectors: Mapping[str, Sequence[float]],
        actor: str,
    ) -> None:
        def write() -> None:
            with self._file.transaction() as conn:
                ids = [
                    r[0]
                    for r in conn.execute(
                        "SELECT chunk_id FROM chunks WHERE collection = ?", (collection,)
                    )
                ]
                if space is not None:
                    missing = [cid for cid in ids if cid not in vectors]
                    if missing:
                        raise EmbeddingMismatch(
                            f"{len(missing)} chunks have no vector in the new space"
                        )
                conn.execute(
                    "DELETE FROM vectors WHERE chunk_id IN "
                    "(SELECT chunk_id FROM chunks WHERE collection = ?)",
                    (collection,),
                )
                if space is not None:
                    conn.executemany(
                        "INSERT INTO vectors VALUES (?, ?, ?)",
                        [(cid, space.key, _pack(vectors[cid])) for cid in ids],
                    )
                updated = conn.execute(
                    "UPDATE collections SET space = ? WHERE name = ?",
                    (space.model_dump_json() if space else None, collection),
                ).rowcount
                if not updated:
                    raise LookupError(f"no collection {collection!r}")
                _log(conn, "space_changed", collection, "", actor, space.key if space else "(none)")

        await asyncio.to_thread(write)

    async def reconcile(self, *, repair: bool) -> ReconcileReport:
        def run() -> ReconcileReport:
            with self._file.transaction() as conn:
                orphan_chunks = conn.execute(
                    "SELECT COUNT(*) FROM chunks "
                    "WHERE source_id NOT IN (SELECT source_id FROM sources)"
                ).fetchone()[0]
                orphan_index = (
                    conn.execute(
                        "SELECT COUNT(*) FROM chunks_fts "
                        "WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks)"
                    ).fetchone()[0]
                    + conn.execute(
                        "SELECT COUNT(*) FROM chunk_identifiers "
                        "WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks)"
                    ).fetchone()[0]
                )
                spaces = {
                    name: EmbeddingSpace.model_validate_json(space).key
                    for name, space in conn.execute(
                        "SELECT name, space FROM collections WHERE space IS NOT NULL"
                    )
                }
                stale = 0
                missing = 0
                for chunk_id, collection in conn.execute(
                    "SELECT chunk_id, collection FROM chunks"
                ).fetchall():
                    key = spaces.get(collection)
                    if (
                        key is not None
                        and not conn.execute(
                            "SELECT 1 FROM vectors WHERE chunk_id = ? AND space = ?",
                            (chunk_id, key),
                        ).fetchone()
                    ):
                        missing += 1
                for chunk_id, space in conn.execute(
                    "SELECT chunk_id, space FROM vectors"
                ).fetchall():
                    row = conn.execute(
                        "SELECT collection FROM chunks WHERE chunk_id = ?", (chunk_id,)
                    ).fetchone()
                    if row is None or spaces.get(row[0]) != space:
                        stale += 1
                        if repair:
                            conn.execute(
                                "DELETE FROM vectors WHERE chunk_id = ? AND space = ?",
                                (chunk_id, space),
                            )
                if repair:
                    conn.execute(
                        "DELETE FROM chunks_fts WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks)"
                    )
                    conn.execute(
                        "DELETE FROM chunk_identifiers "
                        "WHERE chunk_id NOT IN (SELECT chunk_id FROM chunks)"
                    )
                    conn.execute(
                        "DELETE FROM chunks WHERE source_id NOT IN (SELECT source_id FROM sources)"
                    )
                return ReconcileReport(
                    orphan_chunks=orphan_chunks,
                    orphan_index_rows=orphan_index,
                    stale_vectors=stale,
                    missing_vectors=missing,
                )

        return await asyncio.to_thread(run)

    async def stats(self) -> StoreStats:
        def count(table: str) -> int:
            with self._file.reading() as conn:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608  # fixed table names

        values = [
            await asyncio.to_thread(count, t)
            for t in ("collections", "sources", "chunks", "vectors")
        ]
        return StoreStats(
            collections=values[0],
            sources=values[1],
            chunks=values[2],
            vectors=values[3],
            bytes=self._file.path.stat().st_size,
        )

    async def changes(self, *, limit: int = 100) -> list[tuple[datetime, str, str, str, str]]:
        rows = await self._rows(
            "SELECT at, action, collection, source_key, actor FROM knowledge_changes "
            "ORDER BY seq DESC LIMIT ?",
            (limit,),
        )
        return [
            (
                datetime.strptime(at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC),
                action,
                collection,
                key,
                actor,
            )
            for at, action, collection, key, actor in rows
        ]

    # --- helpers --------------------------------------------------------------------------------

    async def _visible_chunks(
        self, access: KnowledgeAccess, condition: str, extra: dict[str, Any]
    ) -> list[Chunk]:
        visible = _visible(access)
        if visible is None:
            return []
        clause, params = visible
        sql = f"""SELECT {_CHUNK_COLUMNS} FROM chunks c JOIN sources s ON s.source_id = c.source_id
                  WHERE {condition} AND {clause} ORDER BY c.source_id, c.ordinal"""  # noqa: S608  # bound values
        return [_chunk(r[0]) for r in await self._rows(sql, {**params, **extra})]

    async def _chunks_by_id(self, ids: Sequence[str]) -> list[Chunk]:
        clause, params = _in("chunk_id", "id", ids)
        rows = await self._rows(f"SELECT data FROM chunks WHERE {clause}", params)  # noqa: S608  # bound values
        return [_chunk(r[0]) for r in rows]

    async def _rows(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any]
    ) -> list[tuple[Any, ...]]:
        def select() -> list[tuple[Any, ...]]:
            with self._file.reading() as conn:
                return conn.execute(sql, params).fetchall()

        return await asyncio.to_thread(select)


def _collection(row: tuple[Any, ...]) -> Collection:
    name, description, space, created_at = row
    return Collection(
        name=name,
        description=description,
        space=EmbeddingSpace.model_validate_json(space) if space else None,
        created_at=datetime.fromisoformat(created_at),
    )


def _delete_source_rows(conn: sqlite3.Connection, source_id: str) -> None:
    ids = "SELECT chunk_id FROM chunks WHERE source_id = ?"
    conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({ids})", (source_id,))  # noqa: S608  # constant subquery
    conn.execute(f"DELETE FROM chunk_identifiers WHERE chunk_id IN ({ids})", (source_id,))  # noqa: S608  # constant subquery
    conn.execute(f"DELETE FROM vectors WHERE chunk_id IN ({ids})", (source_id,))  # noqa: S608  # constant subquery
    conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))
    conn.execute("DELETE FROM sections WHERE source_id = ?", (source_id,))
    conn.execute("DELETE FROM source_labels WHERE source_id = ?", (source_id,))
    conn.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))


def _log(
    conn: sqlite3.Connection, action: str, collection: str, key: str, actor: str, detail: str
) -> None:
    conn.execute(
        "INSERT INTO knowledge_changes (at, action, collection, source_key, actor, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (_now(), action, collection, key, actor, detail),
    )
