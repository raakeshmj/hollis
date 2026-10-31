"""The KnowledgeStore port (ADR-0024).

Contract (tests/knowledge/test_store.py):
- Every read takes a `KnowledgeAccess` and applies it *in the query* before any content is scored
  or returned. An empty grant returns nothing (fail closed).
- `replace_source` swaps a source's sections, chunks, index entries and vectors atomically: a
  reader sees the old version or the new one, never a mix, and never stale chunks afterwards.
- Deleting a source or collection removes its chunks, full-text entries, identifier entries and
  vectors in the same transaction, and appends a content-free entry to the change log.
- Vectors are stored per embedding space; dense search only compares vectors of the requested
  space.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from hollis.knowledge.model import Chunk, Collection, Section, Source
from hollis.runtime.embedding import EmbeddingSpace
from hollis.runtime.knowledge import KnowledgeAccess


class ReconcileReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    orphan_chunks: int = 0
    orphan_index_rows: int = 0
    stale_vectors: int = 0
    missing_vectors: int = 0
    """Chunks of embedded collections without a vector in the collection's space."""


class StoreStats(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    collections: int
    sources: int
    chunks: int
    vectors: int
    bytes: int | None = None


class KnowledgeStore(Protocol):
    async def create_collection(self, collection: Collection, actor: str) -> None: ...

    async def collection(self, name: str) -> Collection | None: ...

    async def collections(self) -> list[Collection]: ...

    async def delete_collection(self, name: str, actor: str, reason: str) -> int: ...

    async def source(self, collection: str, key: str) -> Source | None: ...

    async def sources(self, collection: str) -> list[Source]: ...

    async def replace_source(
        self,
        source: Source,
        sections: Sequence[Section],
        chunks: Sequence[Chunk],
        vectors: Mapping[str, Sequence[float]],
        actor: str,
    ) -> None: ...

    async def delete_source(self, source_id: str, actor: str, reason: str) -> bool: ...

    async def set_labels(self, source_id: str, labels: Sequence[str], actor: str) -> None: ...

    async def search_lexical(
        self, access: KnowledgeAccess, terms: Sequence[str], *, limit: int
    ) -> list[tuple[Chunk, float]]:
        """BM25 matches for any of the (already analyzed) search terms, best first."""
        ...

    async def search_identifiers(
        self, access: KnowledgeAccess, identifiers: Sequence[str], *, limit: int
    ) -> list[tuple[Chunk, int]]:
        """Chunks containing the identifiers exactly (case-insensitive), with how many matched."""
        ...

    async def search_dense(
        self, access: KnowledgeAccess, space: EmbeddingSpace, vector: Sequence[float], *, limit: int
    ) -> list[tuple[Chunk, float]]: ...

    async def chunk(self, access: KnowledgeAccess, chunk_id: str) -> Chunk | None: ...

    async def visible_source(self, access: KnowledgeAccess, source_id: str) -> Source | None: ...

    async def source_chunks(self, access: KnowledgeAccess, source_id: str) -> list[Chunk]: ...

    async def source_sections(self, access: KnowledgeAccess, source_id: str) -> list[Section]: ...

    async def chunks_for_embedding(self, collection: str) -> list[Chunk]: ...

    async def set_space(
        self,
        collection: str,
        space: EmbeddingSpace | None,
        vectors: Mapping[str, Sequence[float]],
        actor: str,
    ) -> None:
        """Switch a collection to a new embedding space with its vectors, atomically, dropping
        vectors of the old space."""
        ...

    async def reconcile(self, *, repair: bool) -> ReconcileReport: ...

    async def stats(self) -> StoreStats: ...

    async def changes(self, *, limit: int = 100) -> list[tuple[datetime, str, str, str, str]]:
        """Recent change-log entries: (at, action, collection, source key, actor)."""
        ...
