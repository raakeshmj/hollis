from pathlib import Path

import pytest

from hollis.adapters.local_embeddings import HashingEmbedder
from hollis.adapters.sqlite_knowledge import SqliteKnowledgeStore
from hollis.knowledge.ingest import KnowledgeBase
from hollis.runtime.knowledge import KnowledgeAccess


@pytest.fixture
async def store(tmp_path: Path) -> SqliteKnowledgeStore:
    return await SqliteKnowledgeStore.open(tmp_path / "knowledge.db")


@pytest.fixture
async def kb(store: SqliteKnowledgeStore) -> KnowledgeBase:
    base = KnowledgeBase(store, embedder=HashingEmbedder(dimensions=256))
    await base.create_collection("docs")
    return base


EVERYONE = KnowledgeAccess(collections=("docs",))
