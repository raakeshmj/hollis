"""The MemoryStore port and an in-process implementation.

Contract (shared by every adapter; tests/memory/test_store_contract.py):
- `save` upserts whole records and appends change-log entries in one atomic step.
- Every query takes the scopes to search and never returns a record outside them.
- `search` ranks by lexical relevance (BM25) over content, subject and tags, among records valid
  and known at `as_of` (`MemoryRecord.valid_at`).
- `delete` removes records and their indexed text, and appends content-free tombstones.
- The change log is append-only.
- Jobs are idempotency markers for multi-step maintenance (e.g. extraction of a run).

The manager serializes writes in-process; like the run store (ADR-0004), stores assume a single
writing process.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from hollis.memory.model import MemoryChange, MemoryRecord, MemoryStatus
from hollis.memory.text import bm25_scores, stem
from hollis.runtime.memory import MemoryKind
from hollis.runtime.text import STOPWORDS, analyze


class MemoryStore(Protocol):
    async def save(
        self, records: Sequence[MemoryRecord], changes: Sequence[MemoryChange]
    ) -> None: ...

    async def get(self, memory_id: str) -> MemoryRecord | None: ...

    async def find(
        self,
        scope: str,
        *,
        subject: str | None = None,
        key: str | None = None,
        content_hash: str | None = None,
        dedup_key: str | None = None,
        statuses: Sequence[MemoryStatus] = (MemoryStatus.ACTIVE,),
    ) -> list[MemoryRecord]:
        """Records in one scope matching every given field (None fields are not filtered)."""
        ...

    async def search(
        self,
        scopes: Sequence[str],
        query: Sequence[str],
        *,
        as_of: datetime,
        kinds: Sequence[MemoryKind] = (),
        limit: int = 50,
    ) -> list[tuple[MemoryRecord, float]]:
        """Lexical matches for any of the query terms, best first, with positive scores."""
        ...

    async def valid(
        self,
        scopes: Sequence[str],
        *,
        as_of: datetime,
        kinds: Sequence[MemoryKind] = (),
        subjects: Sequence[str] = (),
        limit: int = 100,
    ) -> list[MemoryRecord]:
        """Records valid at `as_of`, newest first."""
        ...

    async def records(self, scope: str | None = None) -> list[MemoryRecord]:
        """Every stored record (optionally of one scope), for maintenance and export."""
        ...

    async def subjects(self, scopes: Sequence[str]) -> list[str]:
        """Distinct subjects of active records in `scopes` (for entity-linked expansion)."""
        ...

    async def delete(self, memory_ids: Sequence[str], changes: Sequence[MemoryChange]) -> int: ...

    async def changes(self, memory_id: str) -> list[MemoryChange]: ...

    async def count(self, scope: str) -> int: ...

    async def mark_recalled(self, memory_ids: Sequence[str], at: datetime) -> None: ...

    async def job_done(self, key: str) -> bool: ...

    async def mark_job(self, key: str, at: datetime) -> None: ...


def document_terms(record: MemoryRecord) -> list[str]:
    text = " ".join(filter(None, (record.content, record.subject, record.key, *record.tags)))
    return [stem(t) for t in analyze(text) if t not in STOPWORDS]


class InMemoryMemoryStore:
    """For tests and ephemeral agents. Same semantics as the SQLite store; not thread-safe."""

    def __init__(self) -> None:
        self._records: dict[str, MemoryRecord] = {}
        self._changes: list[MemoryChange] = []
        self._jobs: set[str] = set()

    async def save(self, records: Sequence[MemoryRecord], changes: Sequence[MemoryChange]) -> None:
        for record in records:
            if record.dedup_key is not None:
                clash = next(
                    (
                        r
                        for r in self._records.values()
                        if r.scope == record.scope
                        and r.dedup_key == record.dedup_key
                        and r.memory_id != record.memory_id
                    ),
                    None,
                )
                if clash is not None:
                    raise ValueError(
                        f"dedup_key {record.dedup_key!r} already used in {record.scope}"
                    )
        for record in records:
            self._records[record.memory_id] = record
        self._changes.extend(changes)

    async def get(self, memory_id: str) -> MemoryRecord | None:
        return self._records.get(memory_id)

    async def find(
        self,
        scope: str,
        *,
        subject: str | None = None,
        key: str | None = None,
        content_hash: str | None = None,
        dedup_key: str | None = None,
        statuses: Sequence[MemoryStatus] = (MemoryStatus.ACTIVE,),
    ) -> list[MemoryRecord]:
        return [
            r
            for r in self._records.values()
            if r.scope == scope
            and (not statuses or r.status in statuses)
            and (subject is None or r.subject == subject)
            and (key is None or r.key == key)
            and (content_hash is None or r.content_hash == content_hash)
            and (dedup_key is None or r.dedup_key == dedup_key)
        ]

    async def search(
        self,
        scopes: Sequence[str],
        query: Sequence[str],
        *,
        as_of: datetime,
        kinds: Sequence[MemoryKind] = (),
        limit: int = 50,
    ) -> list[tuple[MemoryRecord, float]]:
        pool = self._valid(scopes, as_of, kinds)
        if not pool or not query:
            return []
        stems = [stem(q) for q in query]
        scores = bm25_scores(stems, [document_terms(r) for r in pool])
        ranked = sorted(
            ((r, s) for r, s in zip(pool, scores, strict=True) if s > 0),
            key=lambda pair: (-pair[1], pair[0].memory_id),
        )
        return ranked[:limit]

    async def valid(
        self,
        scopes: Sequence[str],
        *,
        as_of: datetime,
        kinds: Sequence[MemoryKind] = (),
        subjects: Sequence[str] = (),
        limit: int = 100,
    ) -> list[MemoryRecord]:
        pool = [
            r for r in self._valid(scopes, as_of, kinds) if not subjects or r.subject in subjects
        ]
        pool.sort(key=lambda r: (r.recorded_at, r.memory_id), reverse=True)
        return pool[:limit]

    async def records(self, scope: str | None = None) -> list[MemoryRecord]:
        return [r for r in self._records.values() if scope is None or r.scope == scope]

    async def subjects(self, scopes: Sequence[str]) -> list[str]:
        allowed = set(scopes)
        return sorted(
            {
                r.subject
                for r in self._records.values()
                if r.subject and r.scope in allowed and r.status is MemoryStatus.ACTIVE
            }
        )

    async def delete(self, memory_ids: Sequence[str], changes: Sequence[MemoryChange]) -> int:
        deleted = 0
        for memory_id in memory_ids:
            if self._records.pop(memory_id, None) is not None:
                deleted += 1
        self._changes.extend(changes)
        return deleted

    async def changes(self, memory_id: str) -> list[MemoryChange]:
        return [c for c in self._changes if c.memory_id == memory_id]

    async def count(self, scope: str) -> int:
        return sum(1 for r in self._records.values() if r.scope == scope)

    async def mark_recalled(self, memory_ids: Sequence[str], at: datetime) -> None:
        for memory_id in memory_ids:
            record = self._records.get(memory_id)
            if record is not None:
                self._records[memory_id] = record.model_copy(
                    update={"recall_count": record.recall_count + 1, "last_recalled_at": at}
                )

    async def job_done(self, key: str) -> bool:
        return key in self._jobs

    async def mark_job(self, key: str, at: datetime) -> None:
        self._jobs.add(key)

    def _valid(
        self, scopes: Sequence[str], as_of: datetime, kinds: Sequence[MemoryKind]
    ) -> list[MemoryRecord]:
        allowed = set(scopes)
        return [
            r
            for r in self._records.values()
            if r.scope in allowed and r.valid_at(as_of) and (not kinds or r.kind in kinds)
        ]
