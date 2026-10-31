"""Hybrid knowledge retrieval (ADR-0024).

Pipeline for one query:
1. scope: the requested collections intersected with the caller's grant (empty: nothing);
2. analysis: identifiers and content terms; multi-question input is decomposed (only then);
3. retrievers, run concurrently, each restricted by the store to visible chunks:
   - lexical: BM25 over identifier-analyzed text and section breadcrumbs;
   - dense: cosine over the collection's embedding space (only when the configured embedder's
     space matches the collection's; incompatible spaces are skipped, never compared);
   - exact: chunks containing the query's identifiers verbatim;
4. fusion: reciprocal rank fusion (default) or weighted normalized scores, with an exact-match
   guarantee: chunks containing every identifier in the query rank first, so dense retrieval can
   never bury an exact error code, version or symbol;
5. optional coverage reranking, then de-duplication and a per-source cap for diversity;
6. packing under a passage limit and character budget; citation keys S1..Sn in rank order;
   optional parent expansion with neighbouring chunks of the same section.

The defaults were chosen by the evaluation in docs/architecture/rag-evaluation.md.
"""

import asyncio
import time
from collections import defaultdict
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hollis.knowledge.model import Chunk, Source
from hollis.knowledge.store import KnowledgeStore
from hollis.runtime.embedding import EmbeddingProvider, InputType, embed_all
from hollis.runtime.knowledge import KnowledgeAccess, KnowledgeQuery, RetrievedPassage
from hollis.runtime.text import analyze, identifiers, query_terms


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lexical: bool = True
    dense: bool = True
    exact: bool = True
    fusion: Literal["rrf", "weighted"] = "rrf"
    rrf_k: int = Field(default=60, ge=1)
    lexical_weight: float = Field(default=1.0, ge=0)
    dense_weight: float = Field(default=1.0, ge=0)
    exact_weight: float = Field(default=1.0, ge=0)
    exact_first: bool = True
    """Chunks containing every query identifier rank before all others."""
    candidates: int = Field(default=40, ge=1, le=500)
    """Per retriever and sub-query."""
    rerank: bool = False
    """Coverage reranking: favour chunks containing more of the query's terms."""
    decompose: bool = True
    max_per_source: int = Field(default=3, ge=1)
    expand_parent: bool = False
    parent_chars: int = Field(default=1200, ge=0)


@dataclass
class RankedChunk:
    chunk: Chunk
    score: float
    retrievers: set[str] = field(default_factory=set[str])


@dataclass
class Ranking:
    chunks: list[RankedChunk]
    timings_ms: dict[str, float]
    notes: list[str]


class KnowledgeRetriever:
    """Implements the runtime's `KnowledgeRetrieval` port over a `KnowledgeStore`."""

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        embedder: EmbeddingProvider | None = None,
        config: RetrievalConfig | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._config = config or RetrievalConfig()

    @property
    def config(self) -> RetrievalConfig:
        return self._config

    async def retrieve(self, query: KnowledgeQuery) -> tuple[RetrievedPassage, ...]:
        access = _scoped(query)
        ranking = await self.rank(query.text, access, limit=max(query.limit * 4, 20))
        return await self.pack(ranking.chunks, access, query)

    async def rank(self, text: str, access: KnowledgeAccess, *, limit: int = 20) -> Ranking:
        config = self._config
        timings: dict[str, float] = {}
        notes: list[str] = []
        if not access.collections:
            return Ranking([], timings, ["no accessible collections"])
        subqueries = decompose(text) if config.decompose else [text]
        space = await self._dense_space(access, notes)
        per_query: list[list[RankedChunk]] = []
        for subquery in subqueries:
            lists = await self._retrieve_all(subquery, access, space, timings)
            per_query.append(self._fuse(subquery, lists))
        fused = _interleave(per_query) if len(per_query) > 1 else per_query[0]
        if config.rerank:
            started = time.perf_counter()
            fused = _rerank(text, fused)
            timings["rerank"] = (time.perf_counter() - started) * 1000
        return Ranking(_diversify(fused, config.max_per_source)[:limit], timings, notes)

    async def pack(
        self, ranked: Sequence[RankedChunk], access: KnowledgeAccess, query: KnowledgeQuery
    ) -> tuple[RetrievedPassage, ...]:
        sources: dict[str, Source | None] = {}
        passages: list[RetrievedPassage] = []
        used = 0
        for item in ranked:
            if len(passages) >= query.limit:
                break
            chunk = item.chunk
            if query.exclude_flagged and chunk.flags:
                continue
            if chunk.source_id not in sources:
                sources[chunk.source_id] = await self._store.visible_source(access, chunk.source_id)
            source = sources[chunk.source_id]
            if source is None or source.version != chunk.source_version:
                continue  # deleted, re-versioned or no longer visible since ranking: never shown
            if (
                query.as_of is not None
                and source.effective_date is not None
                and source.effective_date > query.as_of
            ):
                continue
            text = chunk.text
            if self._config.expand_parent:
                text = await self._expand(access, chunk)
            passage = RetrievedPassage(
                key=f"S{len(passages) + 1}",
                chunk_id=chunk.chunk_id,
                source_id=source.source_id,
                collection=source.collection,
                source_title=source.title,
                source_version=source.version,
                fingerprint=source.fingerprint,
                location=chunk.location_label(),
                text=text,
                trust=source.trust,
                labels=source.labels,
                effective_date=source.effective_date,
                ingested_at=source.ingested_at,
                score=round(item.score, 6),
                retrievers=tuple(sorted(item.retrievers)),
                flags=chunk.flags,
            )
            size = len(passage.render())
            if query.max_chars is not None and used + size > query.max_chars:
                continue
            passages.append(passage)
            used += size
        return tuple(passages)

    # --- internals ------------------------------------------------------------------------------

    async def _dense_space(self, access: KnowledgeAccess, notes: list[str]) -> bool:
        if not self._config.dense or self._embedder is None:
            return False
        compatible: list[str] = []
        for name in access.collections:
            collection = await self._store.collection(name)
            if collection is None or collection.space is None:
                continue
            if collection.space == self._embedder.space:
                compatible.append(name)
            else:
                notes.append(
                    f"collection {name!r} uses {collection.space.key}; dense retrieval skipped"
                )
        return bool(compatible)

    async def _retrieve_all(
        self, text: str, access: KnowledgeAccess, dense: bool, timings: dict[str, float]
    ) -> dict[str, list[tuple[Chunk, float]]]:
        config = self._config
        tasks: dict[str, asyncio.Task[list[tuple[Chunk, float]]]] = {}

        async def timed(
            name: str, work: Awaitable[list[tuple[Chunk, float]]]
        ) -> list[tuple[Chunk, float]]:
            started = time.perf_counter()
            result = await work
            timings[name] = timings.get(name, 0.0) + (time.perf_counter() - started) * 1000
            return result

        async with asyncio.TaskGroup() as group:
            if config.lexical:
                tasks["lexical"] = group.create_task(
                    timed(
                        "lexical",
                        self._store.search_lexical(
                            access, query_terms(text), limit=config.candidates
                        ),
                    )
                )
            if dense and self._embedder is not None:
                embedder = self._embedder
                tasks["dense"] = group.create_task(
                    timed("dense", self._dense(embedder, text, access))
                )
            found = identifiers(text)
            if config.exact and found:
                tasks["exact"] = group.create_task(timed("exact", self._exact(found, access)))
        return {name: task.result() for name, task in tasks.items()}

    async def _dense(
        self, embedder: EmbeddingProvider, text: str, access: KnowledgeAccess
    ) -> list[tuple[Chunk, float]]:
        (vector,) = await embed_all(embedder, [text], InputType.QUERY)
        return await self._store.search_dense(
            access, embedder.space, vector, limit=self._config.candidates
        )

    async def _exact(self, found: list[str], access: KnowledgeAccess) -> list[tuple[Chunk, float]]:
        hits = await self._store.search_identifiers(access, found, limit=self._config.candidates)
        return [(chunk, matched / len(found)) for chunk, matched in hits]

    def _fuse(self, text: str, lists: dict[str, list[tuple[Chunk, float]]]) -> list[RankedChunk]:
        config = self._config
        weights = {
            "lexical": config.lexical_weight,
            "dense": config.dense_weight,
            "exact": config.exact_weight,
        }
        merged: dict[str, RankedChunk] = {}
        for name, results in lists.items():
            if not results:
                continue
            best = max(score for _, score in results) or 1.0
            for rank, (chunk, score) in enumerate(results, start=1):
                item = merged.setdefault(chunk.chunk_id, RankedChunk(chunk, 0.0))
                item.retrievers.add(name)
                if config.fusion == "rrf":
                    item.score += weights[name] / (config.rrf_k + rank)
                else:
                    item.score += weights[name] * max(score, 0.0) / best
        ranked = sorted(merged.values(), key=lambda r: (-r.score, r.chunk.chunk_id))
        wanted = {i.casefold() for i in identifiers(text)}
        if config.exact_first and wanted:
            exact = [
                r for r in ranked if wanted <= {i.casefold() for i in identifiers(r.chunk.text)}
            ]
            ids = {r.chunk.chunk_id for r in exact}
            ranked = exact + [r for r in ranked if r.chunk.chunk_id not in ids]
        return ranked

    async def _expand(self, access: KnowledgeAccess, chunk: Chunk) -> str:
        siblings = [
            c
            for c in await self._store.source_chunks(access, chunk.source_id)
            if c.section_id == chunk.section_id
        ]
        if len(siblings) <= 1:
            return chunk.text
        budget = self._config.parent_chars
        index = next(i for i, c in enumerate(siblings) if c.chunk_id == chunk.chunk_id)
        before: list[str] = []
        after: list[str] = []
        left, right = index - 1, index + 1
        while budget > 0 and (left >= 0 or right < len(siblings)):
            if right < len(siblings):
                piece = siblings[right].text
                if len(piece) <= budget:
                    after.append(piece)
                    budget -= len(piece)
                else:
                    budget = 0
                right += 1
            if left >= 0 and budget > 0:
                piece = siblings[left].text
                if len(piece) <= budget:
                    before.insert(0, piece)
                    budget -= len(piece)
                else:
                    budget = 0
                left -= 1
        return "\n\n".join([*before, chunk.text, *after])


def _scoped(query: KnowledgeQuery) -> KnowledgeAccess:
    granted = query.access.collections
    wanted = [c for c in (query.collections or granted) if c in granted]
    return KnowledgeAccess(collections=tuple(wanted), labels=query.access.labels)


def decompose(text: str) -> list[str]:
    """Split only clearly multi-part input: several questions. Single questions stay whole."""
    questions = [q.strip() for q in text.replace("\n", " ").split("?") if len(q.strip()) > 8]
    if len(questions) >= 2:
        return [f"{q}?" for q in questions[:4]]
    return [text]


def _interleave(lists: list[list[RankedChunk]]) -> list[RankedChunk]:
    """Round-robin across sub-query rankings, so every part of the question gets evidence."""
    seen: set[str] = set()
    result: list[RankedChunk] = []
    for depth in range(max(len(r) for r in lists)):
        for ranking in lists:
            if depth < len(ranking) and ranking[depth].chunk.chunk_id not in seen:
                seen.add(ranking[depth].chunk.chunk_id)
                result.append(ranking[depth])
    return result


def _rerank(text: str, ranked: list[RankedChunk]) -> list[RankedChunk]:
    wanted = set(query_terms(text))
    if not wanted or not ranked:
        return ranked
    top = max(r.score for r in ranked) or 1.0
    rescored: list[tuple[float, RankedChunk]] = []
    for position, item in enumerate(ranked):
        present = set(analyze(item.chunk.text + " " + " ".join(item.chunk.path)))
        coverage = len(wanted & present) / len(wanted)
        rescored.append((0.5 * item.score / top + 0.5 * coverage - position * 1e-6, item))
    rescored.sort(key=lambda pair: (-pair[0], pair[1].chunk.chunk_id))
    return [item for _, item in rescored]


def _diversify(ranked: list[RankedChunk], per_source: int) -> list[RankedChunk]:
    counts: dict[str, int] = defaultdict(int)
    seen_hashes: set[str] = set()
    kept: list[RankedChunk] = []
    overflow: list[RankedChunk] = []
    for item in ranked:
        if item.chunk.content_hash in seen_hashes:
            continue
        seen_hashes.add(item.chunk.content_hash)
        if counts[item.chunk.source_id] >= per_source:
            overflow.append(item)
            continue
        counts[item.chunk.source_id] += 1
        kept.append(item)
    return kept + overflow
