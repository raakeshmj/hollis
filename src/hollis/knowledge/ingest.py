"""Knowledge ingestion lifecycle (ADR-0023, ADR-0024).

    kb = KnowledgeBase(await SqliteKnowledgeStore.open("knowledge.db"), embedder=embedder)
    await kb.create_collection("runbooks")
    await kb.sync_directory("runbooks", "docs/runbooks", labels=("group:sre",), actor="ops")

Lifecycle of one source:
1. identity: (collection, key); the key is chosen by the caller (relative path, URL, record id);
2. fingerprint: sha256 of the raw bytes; the *index* fingerprint adds the extractor, chunking
   policy and analyzer versions. If it matches the stored one, nothing happens (idempotent);
3. extraction (content-sniffed), sanitization, structure-aware chunking;
4. embedding (when the collection has an embedding space), done *before* touching the store;
5. an atomic swap of the source's sections, chunks, index entries and vectors. An interruption
   at any earlier step leaves the previous version intact and fully searchable.

Only an operator holding a write grant ingests. Labels (access control), trust and effective dates
come from the caller, never from the document. There is no model-facing ingestion tool.
"""

import asyncio
import fnmatch
import hashlib
import os
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from hollis.knowledge.chunk import ChunkingPolicy, chunk
from hollis.knowledge.document import ExtractedDocument
from hollis.knowledge.extract import UnsupportedFormat, extract
from hollis.knowledge.model import Chunk, Collection, Section, Source
from hollis.knowledge.store import KnowledgeStore, ReconcileReport
from hollis.runtime.embedding import EmbeddingMismatch, EmbeddingProvider, InputType, embed_all
from hollis.runtime.knowledge import LABEL_PATTERN, SourceTrust
from hollis.runtime.redaction import Redactor

ANALYZER_VERSION = "1"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class IngestLimits(_Frozen):
    max_bytes: int = Field(default=20_000_000, ge=1)
    max_chunks: int = Field(default=5000, ge=1)
    max_files: int = Field(default=10_000, ge=1)
    """Per directory sync."""


class IngestResult(_Frozen):
    action: str
    """created, updated, unchanged or rejected."""
    source: Source | None = None
    reason: str = ""


class SyncReport(_Frozen):
    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_labels(labels: Sequence[str]) -> tuple[str, ...]:
    for label in labels:
        if not re.fullmatch(LABEL_PATTERN, label):
            raise ValueError(f"invalid access label {label!r} (expected kind:id)")
    return tuple(sorted(set(labels)))


def contextual_text(source_title: str, chunk_: Chunk) -> str:
    """What is embedded for a chunk: its document title and section path, then its text."""
    header = " > ".join(filter(None, [source_title, *chunk_.path]))
    return f"{header}\n{chunk_.text}" if header else chunk_.text


class KnowledgeBase:
    def __init__(
        self,
        store: KnowledgeStore,
        *,
        embedder: EmbeddingProvider | None = None,
        chunking: ChunkingPolicy | None = None,
        limits: IngestLimits | None = None,
        redactor: Redactor | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """`redactor` should be the gateway's: registered credentials found in documents are
        replaced before indexing."""
        self._store = store
        self._embedder = embedder
        self._chunking = chunking or ChunkingPolicy()
        self._limits = limits or IngestLimits()
        self._redactor = redactor or Redactor()
        self._clock = clock

    @property
    def store(self) -> KnowledgeStore:
        return self._store

    @property
    def embedder(self) -> EmbeddingProvider | None:
        return self._embedder

    async def create_collection(
        self, name: str, description: str = "", *, embedded: bool = True, actor: str = "operator"
    ) -> Collection:
        """`embedded=True` binds the collection to this knowledge base's embedder's space."""
        space = self._embedder.space if embedded and self._embedder is not None else None
        collection = Collection(
            name=name, description=description, space=space, created_at=self._clock()
        )
        await self._store.create_collection(collection, actor)
        return collection

    async def ingest_bytes(
        self,
        collection: str,
        key: str,
        data: bytes,
        *,
        name: str | None = None,
        uri: str | None = None,
        title: str | None = None,
        media_type: str | None = None,
        labels: Sequence[str] = (),
        trust: SourceTrust = SourceTrust.INTERNAL,
        effective_date: datetime | None = None,
        actor: str = "operator",
    ) -> IngestResult:
        target = await self._store.collection(collection)
        if target is None:
            raise LookupError(f"no collection {collection!r}")
        if len(data) > self._limits.max_bytes:
            return IngestResult(
                action="rejected", reason=f"{len(data)} bytes exceeds {self._limits.max_bytes}"
            )
        labels = _validate_labels(labels)
        fingerprint = "sha256:" + hashlib.sha256(data).hexdigest()
        try:
            document = extract(data, name or key, media_type)
        except UnsupportedFormat as exc:
            return IngestResult(action="rejected", reason=str(exc))
        index_fingerprint = self._index_fingerprint(fingerprint, document, target)
        existing = await self._store.source(collection, key)
        unchanged_metadata = existing is not None and (
            existing.labels,
            existing.trust,
            existing.effective_date,
        ) == (labels, trust, effective_date)
        if (
            existing is not None
            and existing.index_fingerprint == index_fingerprint
            and unchanged_metadata
        ):
            return IngestResult(action="unchanged", source=existing)
        drafts = chunk(document, self._chunking)
        if len(drafts.chunks) > self._limits.max_chunks:
            return IngestResult(
                action="rejected",
                reason=f"{len(drafts.chunks)} chunks exceeds {self._limits.max_chunks}",
            )
        source_id = "src_" + hashlib.sha256(f"{collection}\x00{key}".encode()).hexdigest()[:24]
        if existing is None:
            version = 1
        elif existing.fingerprint != fingerprint:
            version = existing.version + 1  # new content
        else:
            version = existing.version  # same content, re-indexed (pipeline or metadata change)
        redacted = 0
        sections = [
            Section(
                section_id=f"{source_id}:s{s.index}",
                source_id=source_id,
                index=s.index,
                title=s.title,
                level=s.level,
                parent=s.parent,
                path=s.path,
                location=s.location,
            )
            for s in drafts.sections
        ]
        chunks: list[Chunk] = []
        for draft in drafts.chunks:
            text = self._redactor.redact_known(draft.text)
            redacted += text != draft.text
            chunks.append(
                Chunk(
                    chunk_id=f"{source_id}:v{version}:c{draft.ordinal}",
                    source_id=source_id,
                    collection=collection,
                    source_version=version,
                    section_id=f"{source_id}:s{draft.section}",
                    ordinal=draft.ordinal,
                    text=text,
                    path=drafts.sections[draft.section].path,
                    kinds=draft.kinds,
                    location=draft.location,
                    flags=draft.flags,
                    tokens=draft.tokens,
                    content_hash="sha256:" + hashlib.sha256(text.encode()).hexdigest(),
                )
            )
        source_title = title or document.title or Path(name or key).name
        vectors = await self._vectors(target, source_title, chunks)
        extraction = {**document.metadata, "extractor": document.extractor}
        if redacted:
            extraction["chunks_with_redacted_secrets"] = str(redacted)
        flagged = sum(1 for c in chunks if c.flags)
        if flagged:
            extraction["chunks_flagged"] = str(flagged)
        source = Source(
            source_id=source_id,
            collection=collection,
            key=key,
            uri=uri,
            title=source_title,
            media_type=document.media_type,
            fingerprint=fingerprint,
            index_fingerprint=index_fingerprint,
            version=version,
            labels=labels,
            trust=trust,
            effective_date=effective_date,
            ingested_at=self._clock(),
            size_bytes=len(data),
            chunk_count=len(chunks),
            extraction=extraction,
        )
        await self._store.replace_source(source, sections, chunks, vectors, actor)
        return IngestResult(action="updated" if existing is not None else "created", source=source)

    async def ingest_file(
        self,
        collection: str,
        path: str | os.PathLike[str],
        *,
        key: str | None = None,
        labels: Sequence[str] = (),
        trust: SourceTrust = SourceTrust.INTERNAL,
        effective_date: datetime | None = None,
        actor: str = "operator",
    ) -> IngestResult:
        file = Path(path)
        size = (await asyncio.to_thread(file.stat)).st_size
        if size > self._limits.max_bytes:
            return IngestResult(
                action="rejected", reason=f"{size} bytes exceeds {self._limits.max_bytes}"
            )
        data = await asyncio.to_thread(file.read_bytes)
        uri = (await asyncio.to_thread(file.resolve)).as_uri()
        return await self.ingest_bytes(
            collection,
            key or file.name,
            data,
            name=file.name,
            uri=uri,
            labels=labels,
            trust=trust,
            effective_date=effective_date,
            actor=actor,
        )

    async def sync_directory(
        self,
        collection: str,
        root: str | os.PathLike[str],
        *,
        include: Sequence[str] = ("*",),
        exclude: Sequence[str] = (
            ".*",
            "*/.*",
            "__pycache__/*",
            "*/__pycache__/*",
            "node_modules/*",
            "*/node_modules/*",
        ),
        labels: Sequence[str] = (),
        trust: SourceTrust = SourceTrust.INTERNAL,
        actor: str = "operator",
    ) -> SyncReport:
        """Make the collection mirror a directory: ingest new and changed files, delete sources
        whose files are gone. Keys are paths relative to `root`. Symlinks are not followed, and
        nothing outside `root` is read."""
        listing = await asyncio.to_thread(_list_files, Path(root), include, exclude)
        seen: set[str] = set()
        created: list[str] = []
        updated: list[str] = []
        unchanged: list[str] = []
        rejected: list[tuple[str, str]] = []
        for index, (relative, path) in enumerate(listing):
            if index >= self._limits.max_files:
                rejected.append((relative, f"more than {self._limits.max_files} files"))
                seen.add(relative)  # over the limit: kept as is, not deleted
                continue
            seen.add(relative)
            result = await self.ingest_file(
                collection, path, key=relative, labels=labels, trust=trust, actor=actor
            )
            buckets = {"created": created, "updated": updated, "unchanged": unchanged}
            buckets.get(result.action, []).append(relative)
            if result.action == "rejected":
                rejected.append((relative, result.reason))
        deleted: list[str] = []
        for source in await self._store.sources(collection):
            if source.key not in seen:
                await self._store.delete_source(
                    source.source_id, actor, "file removed from synced directory"
                )
                deleted.append(source.key)
        return SyncReport(
            created=tuple(created),
            updated=tuple(updated),
            unchanged=tuple(unchanged),
            deleted=tuple(deleted),
            rejected=tuple(rejected),
        )

    async def delete_source(self, collection: str, key: str, *, actor: str, reason: str) -> bool:
        source = await self._store.source(collection, key)
        return source is not None and await self._store.delete_source(
            source.source_id, actor, reason
        )

    async def delete_collection(self, collection: str, *, actor: str, reason: str) -> int:
        return await self._store.delete_collection(collection, actor, reason)

    async def set_labels(
        self, collection: str, key: str, labels: Sequence[str], *, actor: str
    ) -> None:
        source = await self._store.source(collection, key)
        if source is None:
            raise LookupError(f"no source {key!r} in {collection!r}")
        await self._store.set_labels(source.source_id, _validate_labels(labels), actor)

    async def migrate_embeddings(
        self, collection: str, embedder: EmbeddingProvider, *, actor: str
    ) -> int:
        """Re-embed every chunk of a collection in `embedder`'s space and switch to it
        atomically. Until the switch, queries keep using the old space. Returns chunks embedded."""
        target = await self._store.collection(collection)
        if target is None:
            raise LookupError(f"no collection {collection!r}")
        chunks = await self._store.chunks_for_embedding(collection)
        titles = {s.source_id: s.title for s in await self._store.sources(collection)}
        texts = [contextual_text(titles.get(c.source_id, ""), c) for c in chunks]
        vectors = await embed_all(embedder, texts, InputType.DOCUMENT) if texts else []
        await self._store.set_space(
            collection,
            embedder.space,
            {c.chunk_id: v for c, v in zip(chunks, vectors, strict=True)},
            actor,
        )
        self._embedder = embedder
        return len(chunks)

    async def reconcile(self, *, repair: bool = True) -> ReconcileReport:
        return await self._store.reconcile(repair=repair)

    def _index_fingerprint(
        self, fingerprint: str, document: ExtractedDocument, target: Collection
    ) -> str:
        parts = [
            fingerprint,
            document.extractor,
            self._chunking.model_dump_json(),
            ANALYZER_VERSION,
            target.space.key if target.space else "lexical",
        ]
        return "sha256:" + hashlib.sha256("\x00".join(parts).encode()).hexdigest()

    async def _vectors(
        self, target: Collection, title: str, chunks: list[Chunk]
    ) -> dict[str, tuple[float, ...]]:
        if target.space is None:
            return {}
        if self._embedder is None or self._embedder.space != target.space:
            current = self._embedder.space.key if self._embedder is not None else "none"
            raise EmbeddingMismatch(
                f"collection {target.name!r} uses embedding space {target.space.key}; the "
                f"configured embedder is {current}. Re-embed with migrate_embeddings() to change "
                "models."
            )
        if not chunks:
            return {}
        vectors = await embed_all(
            self._embedder, [contextual_text(title, c) for c in chunks], InputType.DOCUMENT
        )
        return {c.chunk_id: v for c, v in zip(chunks, vectors, strict=True)}


def _list_files(
    root: Path, include: Sequence[str], exclude: Sequence[str]
) -> list[tuple[str, Path]]:
    """Regular files under `root` (relative POSIX path, path), sorted. Symlinks are skipped and
    nothing resolving outside `root` is returned."""
    base = root.resolve(strict=True)
    found: list[tuple[str, Path]] = []
    for directory, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames.sort()
        for filename in sorted(filenames):
            path = Path(directory) / filename
            relative = path.relative_to(base).as_posix()
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(base):
                continue
            if not any(fnmatch.fnmatch(relative, p) for p in include):
                continue
            if any(fnmatch.fnmatch(relative, p) for p in exclude):
                continue
            found.append((relative, path))
    return found
