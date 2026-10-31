"""The memory lifecycle: governed writes, ranked recall, deletion and maintenance (ADR-0020).

Writing a candidate:
1. access: the scope must be writable by the caller's `MemoryAccess`;
2. policy: kind, size and credential checks; certainty capped by source;
3. idempotency: a repeated `dedup_key` is a no-op;
4. exact duplicates (same normalized content, slot and kind) are reinforced, not duplicated;
5. slot decision when a subject and key are given:
   - `new` into a slot holding a different active value -> conflict (both kept and linked);
   - `update` -> the old value is superseded and its validity ends now;
   - `correction` -> the old value is retracted;
   - `temporary` -> stored alongside, valid until its end; the base value returns afterwards;
   - a lower-certainty update or correction never replaces a higher-certainty value: it is
     recorded as a conflict instead;
6. the record and its change-log entries are saved atomically.

Recall ranks records valid at the query time by lexical relevance, subject match, certainty,
recency and kind, expands one hop through shared or mentioned subjects (entity links), and
annotates conflicts. It reads
only the scopes it is given; callers pass the run's granted scopes.
"""

import asyncio
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from hollis.memory.model import (
    ChangeAction,
    ChangeKind,
    MemoryCandidate,
    MemoryChange,
    MemoryRecord,
    MemoryStatus,
    Provenance,
    SourceKind,
    WriteAction,
    WriteOutcome,
)
from hollis.memory.policy import MemoryPolicy
from hollis.memory.store import MemoryStore
from hollis.memory.text import content_hash, normalize_subject, phrase
from hollis.runtime.memory import (
    Certainty,
    MemoryAccess,
    MemoryKind,
    RecalledMemory,
    RecallQuery,
)
from hollis.runtime.redaction import Redactor
from hollis.runtime.text import analyze, query_terms

LEXICAL_WEIGHT = 1.0
SUBJECT_WEIGHT = 0.6
CERTAINTY_WEIGHT = 0.15
RECENCY_WEIGHT = 0.2
RECENCY_HALF_LIFE_DAYS = 30.0
KIND_PRIOR = {MemoryKind.PREFERENCE: 0.3, MemoryKind.PROCEDURE: 0.1}
TEMPORARY_BONUS = 0.3
EXPANSION_DECAY = 0.7
HOP_SOURCES = 3
PINNED_KINDS = (MemoryKind.PREFERENCE,)
"""Kinds considered for every recall, whatever the query (preferences shape any task)."""


class AccessDenied(PermissionError):
    pass


class MaintenanceReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    expired: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MemoryManager:
    def __init__(
        self,
        store: MemoryStore,
        *,
        policy: MemoryPolicy | None = None,
        redactor: Redactor | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """`redactor` should be the gateway's, so registered credentials are refused."""
        self._store = store
        self._policy = policy or MemoryPolicy()
        self._redactor = redactor or Redactor()
        self._now = clock
        self._lock = asyncio.Lock()

    @property
    def store(self) -> MemoryStore:
        return self._store

    # --- writes -------------------------------------------------------------------------------

    async def remember(self, candidate: MemoryCandidate, access: MemoryAccess) -> WriteOutcome:
        if not access.can_write(candidate.scope):
            return WriteOutcome(
                action=WriteAction.REJECTED,
                reason=f"scope {candidate.scope!r} is not writable with this access",
            )
        checked = self._policy.check(candidate, self._redactor)
        if isinstance(checked, str):
            return WriteOutcome(action=WriteAction.REJECTED, reason=checked)
        async with self._lock:
            return await self._write(checked)

    async def _write(self, candidate: MemoryCandidate) -> WriteOutcome:
        store, now = self._store, self._now()
        scope = candidate.scope
        if candidate.dedup_key is not None:
            existing = await store.find(scope, dedup_key=candidate.dedup_key, statuses=())
            if existing:
                return WriteOutcome(
                    action=WriteAction.DUPLICATE,
                    record=existing[0],
                    reason="already written with this dedup key",
                )
        subject = normalize_subject(candidate.subject)
        key = normalize_subject(candidate.key)
        digest = content_hash(candidate.content)
        actor = candidate.provenance.label()

        same = [
            r
            for r in await store.find(scope, content_hash=digest)
            if r.subject == subject and r.key == key and r.kind is candidate.kind
        ]
        if same and candidate.change is not ChangeKind.TEMPORARY:
            record = same[0]
            upgraded = max(record.certainty, candidate.certainty, key=lambda c: c.rank)
            updated = record.model_copy(
                update={"reinforced": record.reinforced + 1, "certainty": upgraded}
            )
            detail = f"asserted again by {actor}"
            if upgraded is not record.certainty:
                detail += f"; certainty {record.certainty.value} -> {upgraded.value}"
            await store.save(
                [updated], [self._change(updated, ChangeAction.REINFORCED, actor, detail, now)]
            )
            return WriteOutcome(action=WriteAction.REINFORCED, record=updated, reason=detail)

        if await store.count(scope) >= self._policy.max_records_per_scope:
            await self._maintain_scope(scope, now)
            if await store.count(scope) >= self._policy.max_records_per_scope:
                return WriteOutcome(
                    action=WriteAction.REJECTED,
                    reason=f"scope {scope!r} holds the maximum of "
                    f"{self._policy.max_records_per_scope} memories",
                )

        record = MemoryRecord(
            memory_id=f"mem_{uuid4().hex[:20]}",
            scope=scope,
            kind=candidate.kind,
            content=candidate.content,
            subject=subject,
            key=key,
            certainty=candidate.certainty,
            provenance=candidate.provenance,
            recorded_at=now,
            valid_from=candidate.valid_from,
            valid_until=candidate.valid_until,
            tags=candidate.tags,
            content_hash=digest,
            dedup_key=candidate.dedup_key,
        )
        occupants: list[MemoryRecord] = []
        if subject is not None and key is not None:
            occupants = [
                r
                for r in await store.find(scope, subject=subject, key=key)
                if r.valid_until is None and r.valid_at(now)  # the slot's standing value(s)
            ]
        change = candidate.change

        if not occupants or change is ChangeKind.TEMPORARY:
            note = (
                "nothing to replace" if change in (ChangeKind.UPDATE, ChangeKind.CORRECTION) else ""
            )
            await store.save(
                [record], [self._change(record, ChangeAction.CREATED, actor, note, now)]
            )
            return WriteOutcome(action=WriteAction.CREATED, record=record, reason=note)

        stronger = [r for r in occupants if r.certainty.rank > record.certainty.rank]
        if change is ChangeKind.NEW or stronger:
            reason = (
                "the slot already holds a different value; recorded as a conflict"
                if change is ChangeKind.NEW
                else f"a {record.certainty.value} {change.value} cannot replace a "
                f"{stronger[0].certainty.value} memory; recorded as a conflict"
            )
            ids = tuple(r.memory_id for r in occupants)
            record = record.model_copy(update={"conflicts_with": ids})
            linked = [
                r.model_copy(update={"conflicts_with": (*r.conflicts_with, record.memory_id)})
                for r in occupants
            ]
            changes = [self._change(record, ChangeAction.CREATED, actor, reason, now)]
            changes += [
                self._change(
                    r,
                    ChangeAction.CONFLICT_LINKED,
                    actor,
                    f"contradicted by {record.memory_id}",
                    now,
                )
                for r in linked
            ]
            await store.save([record, *linked], changes)
            return WriteOutcome(
                action=WriteAction.CONFLICT, record=record, affected=ids, reason=reason
            )

        if change is ChangeKind.UPDATE:
            ended = candidate.valid_from or now
            replaced = [
                r.model_copy(
                    update={
                        "status": MemoryStatus.SUPERSEDED,
                        "superseded_by": record.memory_id,
                        "valid_until": max(ended, r.valid_from or ended),
                        "status_reason": f"updated by {actor}",
                    }
                )
                for r in occupants
            ]
            action, change_action, result = (
                "superseded",
                ChangeAction.SUPERSEDED,
                WriteAction.SUPERSEDED,
            )
        else:  # CORRECTION
            replaced = [
                r.model_copy(
                    update={
                        "status": MemoryStatus.RETRACTED,
                        "superseded_by": record.memory_id,
                        "status_reason": f"corrected by {actor}",
                    }
                )
                for r in occupants
            ]
            action, change_action, result = (
                "corrected",
                ChangeAction.RETRACTED,
                WriteAction.CORRECTED,
            )
        record = record.model_copy(update={"supersedes": occupants[0].memory_id})
        changes = [
            self._change(
                record, ChangeAction.CREATED, actor, f"{action} {occupants[0].memory_id}", now
            )
        ]
        changes += [
            self._change(r, change_action, actor, f"by {record.memory_id}", now) for r in replaced
        ]
        await store.save([record, *replaced], changes)
        return WriteOutcome(
            action=result,
            record=record,
            affected=tuple(r.memory_id for r in replaced),
            reason=f"{action} {len(replaced)} memory(ies)",
        )

    async def retract(
        self, memory_id: str, access: MemoryAccess, *, reason: str, actor: str
    ) -> MemoryRecord:
        """Withdraw a memory without deleting it: it stays in history but is never recalled."""
        async with self._lock:
            record = await self._writable(memory_id, access)
            retracted = record.model_copy(
                update={"status": MemoryStatus.RETRACTED, "status_reason": reason}
            )
            await self._store.save(
                [retracted],
                [self._change(retracted, ChangeAction.RETRACTED, actor, reason, self._now())],
            )
            return retracted

    async def forget(
        self, memory_id: str, access: MemoryAccess, *, reason: str, actor: str
    ) -> bool:
        """Delete a memory's content from the memory store, leaving a content-free tombstone.

        This does not erase copies elsewhere: run logs that recalled the memory, model responses
        that repeated it, or the source events it was derived from (ADR-0021)."""
        async with self._lock:
            record = await self._writable(memory_id, access)
            tombstone = self._change(record, ChangeAction.DELETED, actor, reason, self._now())
            return await self._store.delete([record.memory_id], [tombstone]) == 1

    async def forget_subject(
        self, scope: str, subject: str, access: MemoryAccess, *, reason: str, actor: str
    ) -> int:
        """Delete every memory about `subject` in `scope`, whatever its status."""
        if not access.can_write(scope):
            raise AccessDenied(f"scope {scope!r} is not writable with this access")
        normalized = normalize_subject(subject)
        async with self._lock:
            records = await self._store.find(scope, subject=normalized, statuses=())
            now = self._now()
            tombstones = [
                self._change(r, ChangeAction.DELETED, actor, reason, now) for r in records
            ]
            return await self._store.delete([r.memory_id for r in records], tombstones)

    async def history(self, memory_id: str) -> list[MemoryChange]:
        return await self._store.changes(memory_id)

    async def _writable(self, memory_id: str, access: MemoryAccess) -> MemoryRecord:
        record = await self._store.get(memory_id)
        if record is None or not access.can_read(record.scope):
            raise LookupError(f"no memory {memory_id!r} in the accessible scopes")
        if not access.can_write(record.scope):
            raise AccessDenied(f"memory {memory_id!r} is in a read-only scope")
        return record

    # --- recall -------------------------------------------------------------------------------

    async def recall(self, query: RecallQuery) -> tuple[RecalledMemory, ...]:
        """Ranked memories valid at `query.as_of` (default now) in `query.scopes` only."""
        now = self._now()
        as_of = query.as_of or now
        scopes = tuple(dict.fromkeys(query.scopes))
        if not scopes:
            return ()
        words = query_terms(query.text)
        kinds = tuple(query.kinds)
        pool = max(query.limit * 5, 50)
        scored: dict[str, tuple[MemoryRecord, float]] = {}

        hits = await self._store.search(scopes, words, as_of=as_of, kinds=kinds, limit=pool)
        best = max((s for _, s in hits), default=0.0) or 1.0
        for record, lexical in hits:
            scored[record.memory_id] = (record, LEXICAL_WEIGHT * lexical / best)

        subject = normalize_subject(query.subject)
        if subject:
            for record in await self._store.valid(
                scopes, as_of=as_of, kinds=kinds, subjects=[subject], limit=pool
            ):
                scored.setdefault(record.memory_id, (record, 0.0))
        else:
            # One hop: memories about the subjects of the strongest lexical matches, and about
            # known subjects those matches mention ("owned by the Falcon team" links to memories
            # whose subject is "falcon team"). A linked memory inherits a decayed share of the
            # score of the match that linked it.
            top = sorted(scored.values(), key=lambda pair: -pair[1])[:HOP_SOURCES]
            known = {s: f" {phrase(s)} " for s in await self._store.subjects(scopes)}
            known_terms = {s: set(query_terms(s)) for s in known}
            origin: dict[str, float] = {}
            for record, lexical in top:
                linked = {record.subject} if record.subject else set[str]()
                mentioned = f" {phrase(record.content)} "
                content_terms = set(analyze(record.content))
                linked.update(s for s, w in known.items() if len(w) > 4 and w in mentioned)
                # Identifier parts count: CUDA_ERROR_OUT_OF_MEMORY mentions "cuda out of memory".
                linked.update(
                    s
                    for s, words in known_terms.items()
                    if len(words) >= 2 and words <= content_terms
                )
                for linked_subject in linked:
                    origin[linked_subject] = max(origin.get(linked_subject, 0.0), lexical)
            if origin:
                for record in await self._store.valid(
                    scopes, as_of=as_of, kinds=kinds, subjects=sorted(origin), limit=pool
                ):
                    inherited = EXPANSION_DECAY * origin.get(record.subject or "", 0.0)
                    current = scored.get(record.memory_id)
                    if current is None or current[1] < inherited:
                        scored[record.memory_id] = (record, inherited)
        if not kinds or set(kinds) & set(PINNED_KINDS):
            for record in await self._store.valid(
                scopes, as_of=as_of, kinds=PINNED_KINDS, limit=20
            ):
                scored.setdefault(record.memory_id, (record, 0.0))

        asked = set(words)
        ranked: list[tuple[float, MemoryRecord]] = []
        for record, lexical in scored.values():
            if record.scope not in scopes or not record.valid_at(as_of):
                continue  # defence in depth against a store that filters incorrectly
            score = lexical
            subject_terms = set(query_terms(record.subject)) if record.subject else set[str]()
            if record.subject and (
                record.subject == subject or (subject_terms and subject_terms <= asked)
            ):
                score += SUBJECT_WEIGHT  # the whole subject is named in the query
            score += CERTAINTY_WEIGHT * record.certainty.rank
            age_days = max((as_of - record.recorded_at).total_seconds() / 86400, 0.0)
            score += RECENCY_WEIGHT * math.pow(0.5, age_days / RECENCY_HALF_LIFE_DAYS)
            score += KIND_PRIOR.get(record.kind, 0.0)
            if record.valid_until is not None and record.status is MemoryStatus.ACTIVE:
                score += TEMPORARY_BONUS
            ranked.append((score, record))
        ranked.sort(key=lambda pair: (-pair[0], pair[1].memory_id))

        selected: list[RecalledMemory] = []
        used = 0
        for score, record in ranked:
            if len(selected) >= query.limit:
                break
            recalled = _recalled(record, score)
            size = len(recalled.render())
            if query.max_chars is not None and used + size > query.max_chars:
                continue
            selected.append(recalled)
            used += size
        if selected:
            await self._store.mark_recalled([m.memory_id for m in selected], now)
        return tuple(selected)

    # --- maintenance --------------------------------------------------------------------------

    async def maintain(self) -> MaintenanceReport:
        """Expire ended temporary memories and apply retention. Conservative: confirmed memories
        are never deleted, and recently recalled memories are kept."""
        async with self._lock:
            return await self._maintain_scope(None, self._now())

    async def _maintain_scope(self, scope: str | None, now: datetime) -> MaintenanceReport:
        expired: list[MemoryRecord] = []
        doomed: list[MemoryRecord] = []
        policy = self._policy
        for record in await self._store.records(scope):
            if (
                record.status is MemoryStatus.ACTIVE
                and record.valid_until is not None
                and record.valid_until <= now
            ):
                expired.append(
                    record.model_copy(
                        update={"status": MemoryStatus.EXPIRED, "status_reason": "validity ended"}
                    )
                )
                continue
            if record.status is not MemoryStatus.ACTIVE:
                days = policy.history_retention_days
                if days is not None and record.recorded_at < now - timedelta(days=days):
                    doomed.append(record)
                continue
            max_age = policy.max_age_days.get(record.kind)
            if max_age is None or record.certainty is Certainty.CONFIRMED:
                continue
            cutoff = now - timedelta(days=max_age)
            last_used = record.last_recalled_at or record.recorded_at
            if record.recorded_at < cutoff and last_used < cutoff:
                doomed.append(record)
        if expired:
            await self._store.save(
                expired,
                [
                    self._change(r, ChangeAction.EXPIRED, "hollis", "validity ended", now)
                    for r in expired
                ],
            )
        if doomed:
            await self._store.delete(
                [r.memory_id for r in doomed],
                [
                    self._change(r, ChangeAction.DELETED, "hollis", "retention policy", now)
                    for r in doomed
                ],
            )
        return MaintenanceReport(
            expired=tuple(r.memory_id for r in expired),
            deleted=tuple(r.memory_id for r in doomed),
        )

    @staticmethod
    def _change(
        record: MemoryRecord, action: ChangeAction, actor: str, detail: str, at: datetime
    ) -> MemoryChange:
        return MemoryChange(
            memory_id=record.memory_id,
            scope=record.scope,
            at=at,
            action=action,
            actor=actor,
            detail=detail,
        )


def _recalled(record: MemoryRecord, score: float) -> RecalledMemory:
    return RecalledMemory(
        memory_id=record.memory_id,
        scope=record.scope,
        kind=record.kind,
        content=record.content,
        certainty=record.certainty,
        subject=record.subject,
        recorded_at=record.recorded_at,
        valid_from=record.valid_from,
        valid_until=record.valid_until,
        source=record.provenance.label(),
        external=record.provenance.external,
        conflicts_with=record.conflicts_with,
        supersedes=record.supersedes,
        score=round(score, 4),
    )


def user_fact(
    scope: str,
    content: str,
    *,
    kind: MemoryKind = MemoryKind.FACT,
    subject: str | None = None,
    key: str | None = None,
    change: ChangeKind = ChangeKind.NEW,
    actor: str | None = None,
    valid_until: datetime | None = None,
) -> MemoryCandidate:
    """A candidate stated by a person through the application (certainty `confirmed`)."""
    return MemoryCandidate(
        scope=scope,
        kind=kind,
        content=content,
        subject=subject,
        key=key,
        change=change,
        certainty=Certainty.CONFIRMED,
        provenance=Provenance(kind=SourceKind.USER, actor=actor),
        valid_until=valid_until,
    )
