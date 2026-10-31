"""In-process RunStore for tests and ephemeral runs.

Atomicity: `append` performs all checks and mutations without awaiting, so under a single
asyncio event loop no other coroutine can interleave. Not safe to share across threads.
"""

from collections.abc import Sequence
from uuid import UUID

from hollis.runtime.events import EventEnvelope
from hollis.runtime.store import (
    ConcurrencyConflict,
    DuplicateEventId,
    RunListing,
    RunNotFound,
    duplicate_within,
    validate_batch,
)


class InMemoryRunStore:
    def __init__(self) -> None:
        self._logs: dict[UUID, list[EventEnvelope]] = {}
        self._event_ids: set[UUID] = set()

    async def append(
        self, run_id: UUID, expected_version: int, events: Sequence[EventEnvelope]
    ) -> None:
        validate_batch(run_id, expected_version, events)
        current = len(self._logs.get(run_id, ()))
        if current != expected_version:
            raise ConcurrencyConflict(run_id, expected_version, current)
        if (dup := duplicate_within(events)) is not None:
            raise DuplicateEventId(dup)
        for event in events:
            if event.event_id in self._event_ids:
                raise DuplicateEventId(event.event_id)
        self._logs.setdefault(run_id, []).extend(events)
        self._event_ids.update(e.event_id for e in events)

    async def list_runs(self, *, limit: int = 100, offset: int = 0) -> list[RunListing]:
        listings = [
            RunListing(
                run_id=run_id,
                created_at=log[0].occurred_at,
                updated_at=log[-1].occurred_at,
                version=len(log),
            )
            for run_id, log in self._logs.items()
        ]
        listings.sort(key=lambda r: (r.created_at, str(r.run_id)), reverse=True)
        return listings[offset : offset + limit]

    async def read(self, run_id: UUID) -> list[EventEnvelope]:
        try:
            return list(self._logs[run_id])
        except KeyError:
            raise RunNotFound(run_id) from None
