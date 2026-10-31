"""Retrieval evaluation harness: index a corpus, rank every query under each configuration,
and compute graded retrieval metrics against fact-based relevance labels.

This measures retrieval only (which chunks a model would see), not answers. Grade per chunk:
2 if it contains a primary fact, 1 if it contains a supporting fact, else 0.
"""

import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from hollis.adapters.local_embeddings import HashingEmbedder
from hollis.adapters.sqlite_knowledge import SqliteKnowledgeStore
from hollis.knowledge.ingest import KnowledgeBase
from hollis.knowledge.model import Chunk
from hollis.knowledge.retrieve import KnowledgeRetriever, RetrievalConfig
from hollis.runtime.embedding import EmbeddingProvider
from hollis.runtime.knowledge import KnowledgeAccess
from hollis.runtime.text import identifiers
from tests.evals.rag.corpus import Corpus, Query
from tests.evals.rag.simulated import SimulatedSemanticEmbedder

ACCESS = KnowledgeAccess(collections=("eval",))
K = 5

CONFIGS: dict[str, tuple[str, RetrievalConfig]] = {
    "lexical (M4-style terms)": (
        "none",
        RetrievalConfig(dense=False, exact=False, identifier_analysis=False, follow_up=False),
    ),
    "lexical (identifier-aware)": (
        "none",
        RetrievalConfig(dense=False, exact=False, follow_up=False),
    ),
    "lexical + exact": ("none", RetrievalConfig(dense=False, follow_up=False)),
    "lexical + exact + follow-up": ("none", RetrievalConfig(dense=False)),
    "dense (hashing)": ("hashing", RetrievalConfig(lexical=False, exact=False, follow_up=False)),
    "hybrid rrf (hashing)": ("hashing", RetrievalConfig(exact_first=False, follow_up=False)),
    "hybrid rrf + exact-first (hashing)": ("hashing", RetrievalConfig(follow_up=False)),
    "hybrid weighted + exact-first (hashing)": (
        "hashing",
        RetrievalConfig(fusion="weighted", follow_up=False),
    ),
    "hybrid rrf + exact-first + rerank (hashing)": (
        "hashing",
        RetrievalConfig(rerank=True, follow_up=False),
    ),
    "DEFAULT: hybrid rrf + exact-first + follow-up (hashing)": ("hashing", RetrievalConfig()),
    "dense (SIMULATED semantic)": (
        "simulated",
        RetrievalConfig(lexical=False, exact=False, follow_up=False),
    ),
    "hybrid rrf + exact-first (SIMULATED semantic)": (
        "simulated",
        RetrievalConfig(follow_up=False),
    ),
    "DEFAULT config (SIMULATED semantic)": ("simulated", RetrievalConfig()),
}


def norm(text: str) -> str:
    return " ".join(text.split())


def grade(query: Query, chunk: Chunk) -> int:
    text = norm(chunk.text)
    if any(norm(f) in text for f in query.primary):
        return 2
    if any(norm(f) in text for f in query.supporting):
        return 1
    return 0


@dataclass
class QueryResult:
    query: Query
    recall: float
    precision: float
    rr: float
    ndcg: float
    exact_top1: bool | None
    latency_ms: float


@dataclass
class ConfigResult:
    name: str
    results: list[QueryResult] = field(default_factory=list[QueryResult])

    def answerable(self) -> list[QueryResult]:
        return [r for r in self.results if r.query.facts]

    def mean(self, attribute: str, category: str | None = None) -> float:
        rows = [r for r in self.answerable() if category is None or r.query.category == category]
        return statistics.fmean(getattr(r, attribute) for r in rows) if rows else float("nan")

    def exact_rate(self) -> float:
        rows = [r.exact_top1 for r in self.answerable() if r.exact_top1 is not None]
        return sum(rows) / len(rows) if rows else float("nan")

    def latency(self, q: float) -> float:
        values = sorted(r.latency_ms for r in self.results)
        return values[min(int(len(values) * q), len(values) - 1)]


@dataclass
class IndexStats:
    embedder: str
    seconds: float
    chunks: int
    bytes: int
    embedding_calls: int


@dataclass
class Report:
    corpus: str
    configs: list[ConfigResult]
    indexes: list[IndexStats]


async def build(
    corpus: Corpus,
    directory: Path,
    embedder_kind: str,
    embedders: dict[str, EmbeddingProvider] | None = None,
) -> tuple[SqliteKnowledgeStore, EmbeddingProvider | None, IndexStats]:
    embedder: EmbeddingProvider | None = (embedders or {}).get(embedder_kind)
    if embedder is None and embedder_kind == "hashing":
        embedder = HashingEmbedder(dimensions=512)
    elif embedder is None and embedder_kind == "simulated":
        embedder = SimulatedSemanticEmbedder(dimensions=512)
    directory.mkdir(parents=True, exist_ok=True)
    store = await SqliteKnowledgeStore.open(directory / f"{embedder_kind}.db")
    kb = KnowledgeBase(store, embedder=embedder)
    await kb.create_collection("eval", embedded=embedder is not None)
    started = time.perf_counter()
    for doc in corpus.docs:
        result = await kb.ingest_bytes(
            "eval", doc.key, doc.data, effective_date=doc.effective, labels=doc.labels
        )
        if result.action == "rejected":
            raise AssertionError(f"{doc.key}: {result.reason}")
    seconds = time.perf_counter() - started
    stats = await store.stats()
    calls = embedder.calls if isinstance(embedder, HashingEmbedder) else 0
    return (
        store,
        embedder,
        IndexStats(embedder_kind, seconds, stats.chunks, stats.bytes or 0, calls),
    )


def _ndcg(grades: list[int], ideal: list[int], k: int = 10) -> float:
    def dcg(values: list[int]) -> float:
        return sum((2**g - 1) / math.log2(i + 2) for i, g in enumerate(values[:k]))

    best = dcg(sorted(ideal, reverse=True))
    return dcg(grades) / best if best else 0.0


async def evaluate(
    corpus: Corpus,
    directory: Path,
    configs: dict[str, tuple[str, RetrievalConfig]] | None = None,
    *,
    embedders: dict[str, EmbeddingProvider] | None = None,
) -> Report:
    configs = configs or CONFIGS
    indexes: dict[str, tuple[SqliteKnowledgeStore, EmbeddingProvider | None, IndexStats]] = {}
    for kind in dict.fromkeys(kind for kind, _ in configs.values()):
        indexes[kind] = await build(corpus, directory / corpus.name, kind, embedders)
    results: list[ConfigResult] = []
    for name, (kind, config) in configs.items():
        store, embedder, _ = indexes[kind]
        all_chunks = await store.chunks_for_embedding("eval")
        retriever = KnowledgeRetriever(store, embedder=embedder, config=config)
        outcome = ConfigResult(name)
        for query in corpus.queries:
            started = time.perf_counter()
            ranking = await retriever.rank(query.text, ACCESS, limit=10)
            latency = (time.perf_counter() - started) * 1000
            ranked = [r.chunk for r in ranking.chunks]
            top = ranked[:K]
            grades = [grade(query, c) for c in ranked]
            covered = sum(1 for f in query.facts if any(norm(f) in norm(c.text) for c in top))
            first = next((i for i, g in enumerate(grades) if g > 0), None)
            ideal = [g for g in (grade(query, c) for c in all_chunks) if g > 0]
            exact_top1 = None
            if query.category in ("exact_identifier", "error_code", "version") and identifiers(
                query.text
            ):
                exact_top1 = bool(grades) and grades[0] == 2
            outcome.results.append(
                QueryResult(
                    query=query,
                    recall=covered / len(query.facts) if query.facts else 0.0,
                    precision=sum(1 for g in grades[:K] if g > 0) / K,
                    rr=1 / (first + 1) if first is not None else 0.0,
                    ndcg=_ndcg(grades, ideal),
                    exact_top1=exact_top1,
                    latency_ms=latency,
                )
            )
        results.append(outcome)
    return Report(corpus.name, results, [stats for _, _, stats in indexes.values()])


CATEGORY_ORDER = [
    "exact_identifier",
    "error_code",
    "version",
    "paraphrase",
    "synonym",
    "identifier_to_concept",
    "multi_hop",
    "cross_document",
    "temporal",
    "conflicting",
    "distractor",
]


def markdown(report: Report) -> str:
    lines = [
        f"#### {report.corpus}",
        "",
        "| configuration | Recall@5 | Precision@5 | MRR | nDCG@10 | exact@1 | p50 ms | p95 ms |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in report.configs:
        lines.append(
            f"| {c.name} | {c.mean('recall'):.2f} | {c.mean('precision'):.2f} | {c.mean('rr'):.2f} "
            f"| {c.mean('ndcg'):.2f} | {c.exact_rate():.2f} "
            f"| {c.latency(0.5):.1f} | {c.latency(0.95):.1f} |"
        )
    present = [
        cat
        for cat in CATEGORY_ORDER
        if any(r.query.category == cat for r in report.configs[0].results)
    ]
    lines += [
        "",
        "Recall@5 by category:",
        "",
        "| configuration | " + " | ".join(present) + " |",
        "|---|" + "---|" * len(present),
    ]
    for c in report.configs:
        lines.append(
            f"| {c.name} | " + " | ".join(f"{c.mean('recall', cat):.2f}" for cat in present) + " |"
        )
    lines += [
        "",
        "| index | build s | chunks | size KiB | embedding calls |",
        "|---|---|---|---|---|",
    ]
    for s in report.indexes:
        lines.append(
            f"| {s.embedder} | {s.seconds:.2f} | {s.chunks} "
            f"| {s.bytes / 1024:.0f} | {s.embedding_calls} |"
        )
    return "\n".join(lines)
