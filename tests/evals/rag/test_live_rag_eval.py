"""Opt-in live RAG evaluation: real embeddings and real answers through the gateway.

Skipped unless HOLLIS_EVAL_BASE_URL and HOLLIS_EVAL_MODEL (a text-generation deployment) are set.
With HOLLIS_EVAL_EMBED_MODEL and HOLLIS_EVAL_EMBED_DIMENSIONS (and optionally
HOLLIS_EVAL_EMBED_BASE_URL), retrieval uses that real embedding model, which is the only
validation of dense *semantic* quality. Example with a local Ollama:

    HOLLIS_EVAL_BASE_URL=http://127.0.0.1:11434/v1 HOLLIS_EVAL_MODEL=qwen3:8b \\
    HOLLIS_EVAL_PROVIDER=ollama HOLLIS_EVAL_EMBED_MODEL=nomic-embed-text \\
    HOLLIS_EVAL_EMBED_DIMENSIONS=768 uv run pytest tests/evals/rag -m live -ra -s

For each query the runner answers with knowledge retrieved at run start; the answer is graded by
keywords (`Query.answer`), unanswerable queries must abstain, and the grounding report gives
citation precision and fabricated citations. Not run yet: no inference server was available.
"""

import os
import statistics
from pathlib import Path
from typing import Any

import pytest

from hollis.adapters.local_embeddings import HashingEmbedder
from hollis.adapters.memory_store import InMemoryRunStore
from hollis.adapters.sqlite_knowledge import SqliteKnowledgeStore
from hollis.gateway import Catalog, ModelGateway
from hollis.knowledge.ingest import KnowledgeBase
from hollis.knowledge.retrieve import KnowledgeRetriever
from hollis.runtime.embedding import EmbeddingProvider
from hollis.runtime.grounding import Support
from hollis.runtime.knowledge import KnowledgeAccess
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import RunSpec
from tests.evals.rag.corpus import engineering
from tests.evals.rag.harness import evaluate, markdown

pytestmark = pytest.mark.live
INSTRUCTIONS = (
    "Answer from the knowledge passages only, citing the passages you use by key like [S1]. "
    "If they do not answer the question, say that the sources do not say."
)


def _catalog() -> Catalog:
    base = os.environ["HOLLIS_EVAL_BASE_URL"]
    endpoint: dict[str, Any] = {"base_url": base}
    if auth := os.environ.get("HOLLIS_EVAL_AUTH_ENV"):
        endpoint["auth"] = {"type": "bearer", "env": auth}
    locality = os.environ.get("HOLLIS_EVAL_LOCALITY", "local")
    deployments: list[dict[str, Any]] = [
        {
            "id": "eval",
            "protocol": os.environ.get("HOLLIS_EVAL_PROTOCOL", "openai-chat-completions"),
            "provider": os.environ.get("HOLLIS_EVAL_PROVIDER", "unknown"),
            "model": os.environ["HOLLIS_EVAL_MODEL"],
            "locality": locality,
            "endpoint": endpoint,
        }
    ]
    if embed := os.environ.get("HOLLIS_EVAL_EMBED_MODEL"):
        deployments.append(
            {
                "id": "eval-embed",
                "protocol": "openai-embeddings",
                "provider": os.environ.get("HOLLIS_EVAL_PROVIDER", "unknown"),
                "model": embed,
                "locality": locality,
                "endpoint": {
                    **endpoint,
                    "base_url": os.environ.get("HOLLIS_EVAL_EMBED_BASE_URL", base),
                },
                "capabilities": {
                    "embedding": {"dimensions": int(os.environ["HOLLIS_EVAL_EMBED_DIMENSIONS"])}
                },
            }
        )
    return Catalog.from_mapping({"deployments": deployments})


@pytest.mark.skipif(
    not (os.environ.get("HOLLIS_EVAL_BASE_URL") and os.environ.get("HOLLIS_EVAL_MODEL")),
    reason="set HOLLIS_EVAL_BASE_URL and HOLLIS_EVAL_MODEL to run the live RAG evaluation",
)
async def test_live_rag(tmp_path: Path) -> None:
    gateway = ModelGateway(_catalog())
    embedder: EmbeddingProvider = (
        gateway.embedder("eval-embed")
        if os.environ.get("HOLLIS_EVAL_EMBED_MODEL")
        else HashingEmbedder()
    )
    corpus = engineering()
    try:
        if os.environ.get("HOLLIS_EVAL_EMBED_MODEL"):
            from hollis.knowledge.retrieve import RetrievalConfig

            configs = {
                "dense (live embedding)": (
                    "live",
                    RetrievalConfig(lexical=False, exact=False, follow_up=False),
                ),
                "DEFAULT (live embedding)": ("live", RetrievalConfig()),
            }
            print(
                markdown(
                    await evaluate(
                        corpus, tmp_path / "retrieval", configs, embedders={"live": embedder}
                    )
                )
            )
        store = await SqliteKnowledgeStore.open(tmp_path / "kb.db")
        kb = KnowledgeBase(store, embedder=embedder)
        await kb.create_collection("eval")
        for doc in corpus.docs:
            await kb.ingest_bytes("eval", doc.key, doc.data, effective_date=doc.effective)
        runner = gateway.runner(
            InMemoryRunStore(), knowledge=KnowledgeRetriever(store, embedder=embedder)
        )
        correct = 0
        precision: list[float] = []
        fabricated = 0
        for query in corpus.queries:
            run = await runner.create(
                RunSpec(
                    model="eval",
                    input=query.text,
                    instructions=INSTRUCTIONS,
                    budget=RunBudget(max_output_tokens=300),
                    knowledge=KnowledgeAccess(collections=("eval",)),
                )
            )
            final = await runner.execute(run.run_id)
            text = (final.response.content if final.response else "").casefold()
            report = final.grounding
            ok = (
                all(k in text for k in query.answer)
                if query.answer
                else bool(report and report.abstained)
            )
            correct += ok
            if report is not None:
                fabricated += len(report.fabricated)
                if report.citation_precision is not None:
                    precision.append(report.citation_precision)
            supported = report.count(Support.SUPPORTED) if report else 0
            print(f"{query.qid:22} {'OK ' if ok else 'MISS'} supported={supported} {text[:90]!r}")
        print(
            f"correct {correct}/{len(corpus.queries)}, fabricated citations {fabricated}, "
            f"mean citation precision "
            f"{statistics.fmean(precision) if precision else float('nan'):.2f}"
        )
    finally:
        await gateway.aclose()
