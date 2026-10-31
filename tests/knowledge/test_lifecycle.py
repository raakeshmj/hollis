"""Ingestion lifecycle: idempotency, atomic replacement, sync, deletion, migration, reconcile."""

import os
import sqlite3
from pathlib import Path

import pytest

from hollis.adapters.local_embeddings import HashingEmbedder
from hollis.adapters.sqlite_knowledge import SqliteKnowledgeStore
from hollis.knowledge.ingest import IngestLimits, KnowledgeBase
from hollis.runtime.embedding import EmbeddingMismatch
from hollis.runtime.knowledge import KnowledgeAccess, SourceTrust
from hollis.runtime.redaction import Redactor
from tests.knowledge.conftest import EVERYONE

V1 = b"# Deploy guide\n\nDeploys run through ArgoCD in region eu-west-1.\n"
V2 = b"# Deploy guide\n\nDeploys run through Flux in region eu-central-1.\n"


async def _texts(
    store: SqliteKnowledgeStore, term: str, access: KnowledgeAccess = EVERYONE
) -> list[str]:
    return [c.text for c, _ in await store.search_lexical(access, [term], limit=20)]


async def test_reingesting_unchanged_content_is_a_no_op(
    kb: KnowledgeBase, store: SqliteKnowledgeStore
) -> None:
    first = await kb.ingest_bytes("docs", "deploy.md", V1)
    again = await kb.ingest_bytes("docs", "deploy.md", V1)
    assert (first.action, again.action) == ("created", "unchanged")
    assert first.source is not None
    assert first.source.version == 1
    assert (await store.stats()).chunks == first.source.chunk_count


async def test_updates_replace_old_chunks_atomically(
    kb: KnowledgeBase, store: SqliteKnowledgeStore
) -> None:
    await kb.ingest_bytes("docs", "deploy.md", V1)
    updated = await kb.ingest_bytes("docs", "deploy.md", V2)
    assert updated.action == "updated"
    assert updated.source is not None
    assert updated.source.version == 2
    assert await _texts(store, "ArgoCD") == []  # the old version is gone from every index
    assert await store.search_identifiers(EVERYONE, ["eu-west-1"], limit=5) == []
    assert await _texts(store, "Flux") == ["Deploys run through Flux in region eu-central-1."]
    stats = await store.stats()
    assert stats.chunks == stats.vectors == updated.source.chunk_count


async def test_governance_changes_do_not_create_versions(
    kb: KnowledgeBase, store: SqliteKnowledgeStore
) -> None:
    await kb.ingest_bytes("docs", "deploy.md", V1)
    relabelled = await kb.ingest_bytes(
        "docs", "deploy.md", V1, labels=("group:sre",), trust=SourceTrust.CURATED
    )
    assert relabelled.action == "updated"
    assert relabelled.source is not None
    assert relabelled.source.version == 1
    assert relabelled.source.labels == ("group:sre",)
    assert await _texts(store, "ArgoCD") == []  # now restricted
    assert await _texts(
        store, "ArgoCD", KnowledgeAccess(collections=("docs",), labels=("group:sre",))
    )


async def test_directory_sync_mirrors_the_tree(
    kb: KnowledgeBase, store: SqliteKnowledgeStore, tmp_path: Path
) -> None:
    root = tmp_path / "corpus"
    (root / "guides").mkdir(parents=True)
    (root / "guides" / "deploy.md").write_bytes(V1)
    (root / "notes.txt").write_text("Rotate keys quarterly.")
    (root / ".secret").write_text("hidden file")
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\nmust never be indexed")
    os.symlink(outside, root / "link.md")
    first = await kb.sync_directory("docs", root)
    assert sorted(first.created) == ["guides/deploy.md", "notes.txt"]

    (root / "guides" / "deploy.md").write_bytes(V2)
    (root / "notes.txt").unlink()
    (root / "new.md").write_text("# New\n\nfresh content")
    second = await kb.sync_directory("docs", root)
    assert second.updated == ("guides/deploy.md",)
    assert second.created == ("new.md",)
    assert second.deleted == ("notes.txt",)
    assert await _texts(store, "quarterly") == []
    assert await _texts(store, "never") == []
    assert sorted(s.key for s in await store.sources("docs")) == ["guides/deploy.md", "new.md"]


async def test_deleting_a_source_clears_every_index(
    kb: KnowledgeBase, store: SqliteKnowledgeStore, tmp_path: Path
) -> None:
    await kb.ingest_bytes("docs", "deploy.md", V1 + b"\nToken parse uses PARSE_ERR_42.\n")
    assert await kb.delete_source("docs", "deploy.md", actor="alice", reason="obsolete")
    assert await _texts(store, "ArgoCD") == []
    assert await store.search_identifiers(EVERYONE, ["PARSE_ERR_42"], limit=5) == []
    stats = await store.stats()
    assert (stats.sources, stats.chunks, stats.vectors) == (0, 0, 0)
    changes = await store.changes()
    assert changes[0][1:] == ("source_deleted", "docs", "deploy.md", "alice")
    db = tmp_path / "knowledge.db"
    assert b"ArgoCD" not in db.read_bytes()  # secure_delete overwrote the freed pages
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.DatabaseError, match="append-only"):
        conn.execute("DELETE FROM knowledge_changes")


async def test_deleting_a_collection(kb: KnowledgeBase, store: SqliteKnowledgeStore) -> None:
    await kb.ingest_bytes("docs", "a.md", V1)
    await kb.ingest_bytes("docs", "b.md", V2)
    assert await kb.delete_collection("docs", actor="ops", reason="retired") == 2
    assert await store.collection("docs") is None
    assert (await store.stats()).chunks == 0


async def test_embedding_spaces_are_never_mixed(store: SqliteKnowledgeStore) -> None:
    small = KnowledgeBase(store, embedder=HashingEmbedder(dimensions=64))
    await small.create_collection("docs")
    await small.ingest_bytes("docs", "a.md", V1)
    other = KnowledgeBase(store, embedder=HashingEmbedder(dimensions=128))
    with pytest.raises(EmbeddingMismatch, match="migrate_embeddings"):
        await other.ingest_bytes("docs", "b.md", V2)
    collection = await store.collection("docs")
    assert collection is not None
    assert collection.space is not None
    with pytest.raises(EmbeddingMismatch):
        await store.search_dense(EVERYONE, collection.space, [0.0] * 128, limit=3)


async def test_migrating_embeddings_switches_spaces_atomically(store: SqliteKnowledgeStore) -> None:
    old = HashingEmbedder(dimensions=64)
    kb = KnowledgeBase(store, embedder=old)
    await kb.create_collection("docs")
    await kb.ingest_bytes("docs", "a.md", V1)
    new = HashingEmbedder(dimensions=128, deployment="hashing-v2")
    assert await kb.migrate_embeddings("docs", new, actor="ops") == 1
    collection = await store.collection("docs")
    assert collection is not None
    assert collection.space == new.space
    assert await store.search_dense(EVERYONE, old.space, [0.0] * 64, limit=3) == []
    hits = await store.search_dense(EVERYONE, new.space, new.vector("ArgoCD deploys"), limit=3)
    assert len(hits) == 1
    report = await kb.reconcile(repair=False)
    assert (report.stale_vectors, report.missing_vectors) == (0, 0)


async def test_reconcile_repairs_orphans(
    kb: KnowledgeBase, store: SqliteKnowledgeStore, tmp_path: Path
) -> None:
    await kb.ingest_bytes("docs", "a.md", V1)
    with sqlite3.connect(tmp_path / "knowledge.db") as conn:
        conn.execute(
            "INSERT INTO chunks_fts (chunk_id, body, path) VALUES ('ghost', 'ghost text', '')"
        )
        conn.execute("INSERT INTO vectors VALUES ('ghost', 'emb_x', x'00000000')")
    report = await kb.reconcile(repair=True)
    assert report.orphan_index_rows == 1
    assert report.stale_vectors == 1
    assert await kb.reconcile(repair=False) == type(report)()


async def test_registered_secrets_are_redacted_before_indexing(
    store: SqliteKnowledgeStore, tmp_path: Path
) -> None:
    secret = "ghp_0123456789abcdefSECRETvalue"
    kb = KnowledgeBase(store, redactor=Redactor([secret]))
    await kb.create_collection("docs", embedded=False)
    result = await kb.ingest_bytes(
        "docs", "env.md", f"# Env\n\nUse token {secret} for CI.\n".encode()
    )
    assert result.source is not None
    assert result.source.extraction["chunks_with_redacted_secrets"] == "1"
    assert secret.encode() not in (tmp_path / "knowledge.db").read_bytes()


async def test_limits_reject_oversized_sources(store: SqliteKnowledgeStore) -> None:
    kb = KnowledgeBase(store, limits=IngestLimits(max_bytes=100, max_chunks=1))
    await kb.create_collection("docs", embedded=False)
    assert (await kb.ingest_bytes("docs", "big.md", b"x" * 200)).action == "rejected"
    many = b"\n\n".join(b"# Section %d\n\nbody text" % i for i in range(3))
    tiny = KnowledgeBase(store, limits=IngestLimits(max_chunks=1))
    assert (await tiny.ingest_bytes("docs", "many.md", many)).action == "rejected"
    assert (await kb.ingest_bytes("docs", "bin.dat", b"\x00\x01")).reason.endswith("not supported")
