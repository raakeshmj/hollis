"""Every MemoryStore adapter: scope isolation, temporal validity, deletion and the change log."""

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from hollis.adapters.sqlite_memory import SqliteMemoryStore
from hollis.memory import (
    ChangeAction,
    MemoryChange,
    MemoryRecord,
    MemoryStatus,
    MemoryStore,
    Provenance,
    SourceKind,
)
from hollis.memory.text import content_hash
from hollis.runtime.memory import Certainty, MemoryKind
from tests.memory.conftest import T0


def _record(
    memory_id: str, content: str, scope: str = "user:alice", **fields: object
) -> MemoryRecord:
    return MemoryRecord.model_validate(
        {
            "memory_id": memory_id,
            "scope": scope,
            "kind": MemoryKind.FACT,
            "content": content,
            "certainty": Certainty.CONFIRMED,
            "provenance": Provenance(kind=SourceKind.USER),
            "recorded_at": T0,
            "content_hash": content_hash(content),
        }
        | fields
    )


def _change(record: MemoryRecord, action: ChangeAction = ChangeAction.CREATED) -> MemoryChange:
    return MemoryChange(
        memory_id=record.memory_id, scope=record.scope, at=T0, action=action, actor="t"
    )


async def test_round_trip_and_find(memory_store: MemoryStore) -> None:
    record = _record("m1", "Deploys go through ArgoCD", subject="deploy", key="tool", tags=("ops",))
    await memory_store.save([record], [_change(record)])
    assert await memory_store.get("m1") == record
    assert await memory_store.find("user:alice", subject="deploy", key="tool") == [record]
    assert await memory_store.find("user:bob", subject="deploy") == []
    assert await memory_store.count("user:alice") == 1


async def test_search_never_crosses_scopes(memory_store: MemoryStore) -> None:
    mine = _record("a", "The staging database password rotates monthly")
    theirs = _record("b", "The staging database is postgres 16", scope="user:bob")
    await memory_store.save([mine, theirs], [])
    hits = await memory_store.search(["user:alice"], ["staging", "database"], as_of=T0)
    assert [r.memory_id for r, _ in hits] == ["a"]
    both = await memory_store.search(["user:alice", "user:bob"], ["staging"], as_of=T0)
    assert {r.memory_id for r, _ in both} == {"a", "b"}
    assert all(score > 0 for _, score in both)
    assert await memory_store.valid(["user:carol"], as_of=T0) == []


async def test_search_ranks_by_relevance_and_stems(memory_store: MemoryStore) -> None:
    await memory_store.save(
        [
            _record("x", "Deploying requires approval from the release manager"),
            _record("y", "Lunch is at noon"),
            _record("z", "Deployment approvals expire after one day; deploy windows are 9-17"),
        ],
        [],
    )
    hits = await memory_store.search(["user:alice"], ["deploy", "approval"], as_of=T0)
    assert {r.memory_id for r, _ in hits} == {"x", "z"}


async def test_temporal_validity_and_status(memory_store: MemoryStore) -> None:
    later = T0 + timedelta(days=5)
    await memory_store.save(
        [
            _record(
                "old", "Region is eu-west-1", status=MemoryStatus.SUPERSEDED, valid_until=later
            ),
            _record("new", "Region is eu-central-1", recorded_at=later, valid_from=later),
            _record("wrong", "Region is us-east-1", status=MemoryStatus.RETRACTED),
        ],
        [],
    )
    at_start = {
        r.memory_id for r, _ in await memory_store.search(["user:alice"], ["region"], as_of=T0)
    }
    at_end = {
        r.memory_id for r, _ in await memory_store.search(["user:alice"], ["region"], as_of=later)
    }
    assert at_start == {"old"}
    assert at_end == {"new"}


async def test_delete_removes_text_from_the_index(memory_store: MemoryStore) -> None:
    record = _record("gone", "Alice's home address is 12 Elm Street")
    await memory_store.save([record], [_change(record)])
    assert await memory_store.delete(["gone"], [_change(record, ChangeAction.DELETED)]) == 1
    assert await memory_store.get("gone") is None
    assert await memory_store.search(["user:alice"], ["elm", "address"], as_of=T0) == []
    assert [c.action for c in await memory_store.changes("gone")] == [
        ChangeAction.CREATED,
        ChangeAction.DELETED,
    ]


async def test_dedup_keys_are_unique_per_scope(memory_store: MemoryStore) -> None:
    await memory_store.save([_record("a", "one", dedup_key="k")], [])
    await memory_store.save(
        [_record("b", "two", scope="user:bob", dedup_key="k")], []
    )  # other scope
    with pytest.raises(ValueError, match=r"dedup_key|rejected"):
        await memory_store.save([_record("c", "three", dedup_key="k")], [])


async def test_jobs_and_recall_marks(memory_store: MemoryStore) -> None:
    assert not await memory_store.job_done("extract:1")
    await memory_store.mark_job("extract:1", T0)
    assert await memory_store.job_done("extract:1")
    await memory_store.save([_record("m", "x marks")], [])
    await memory_store.mark_recalled(["m", "missing"], T0)
    marked = await memory_store.get("m")
    assert marked is not None
    assert marked.recall_count == 1
    assert marked.last_recalled_at == T0


async def test_sqlite_change_log_is_append_only_and_deleted_text_is_gone(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    store = await SqliteMemoryStore.open(path)
    secret_text = "UNIQUE-SENSITIVE-PHRASE-4711"
    record = _record("s", f"Note: {secret_text}")
    await store.save([record], [_change(record)])
    await store.delete(["s"], [_change(record, ChangeAction.DELETED)])
    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM memory_changes")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE memory_changes SET detail = 'x'")
    assert secret_text.encode() not in path.read_bytes()  # secure_delete overwrote it
    assert path.stat().st_mode & 0o777 == 0o600
