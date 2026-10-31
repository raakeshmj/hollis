"""The RunStore port: durable, append-only storage of run event logs.

Contract (shared by every adapter and enforced by tests/adapters/test_store_contract.py):

- `append` is atomic: every event in the batch is stored, or none is.
- Checks run in this order: batch validation (`InvalidAppend`), optimistic concurrency
  (`ConcurrencyConflict` unless the run's current version equals `expected_version`), then
  event-id uniqueness across the whole store (`DuplicateEventId`). An append never silently
  succeeds against stale state, and a retried append of an already-committed batch surfaces
  as `ConcurrencyConflict`, so callers re-read and re-fold instead of being deduplicated.
- Creating a run is `append(run_id, 0, [RunCreated...])`; creating an existing run conflicts.
- `read` returns events in sequence order, or raises `RunNotFound`.
- Stores do not interpret event semantics; callers validate with `state.apply` before appending.
"""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from hollis.runtime.events import EventEnvelope
from hollis.runtime.state import RunState, fold


class StoreError(Exception):
    """Base class for RunStore failures."""


class InvalidAppend(StoreError, ValueError):
    """The batch itself is malformed (empty, wrong run, non-contiguous sequences)."""


class ConcurrencyConflict(StoreError):
    def __init__(self, run_id: UUID, expected: int, actual: int) -> None:
        self.run_id = run_id
        self.expected = expected
        self.actual = actual
        super().__init__(f"run {run_id}: expected version {expected}, actual {actual}")


class DuplicateEventId(StoreError):
    def __init__(self, event_id: UUID) -> None:
        self.event_id = event_id
        super().__init__(f"event_id {event_id} already exists")


class RunNotFound(StoreError):
    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__(f"run {run_id} not found")


class RunStore(Protocol):
    async def append(
        self, run_id: UUID, expected_version: int, events: Sequence[EventEnvelope]
    ) -> None: ...

    async def read(self, run_id: UUID) -> list[EventEnvelope]: ...


def validate_batch(run_id: UUID, expected_version: int, events: Sequence[EventEnvelope]) -> None:
    """Adapter-independent batch checks; raises InvalidAppend or DuplicateEventId."""
    if expected_version < 0:
        raise InvalidAppend(f"expected_version must be >= 0, got {expected_version}")
    if not events:
        raise InvalidAppend("cannot append an empty batch")
    for offset, event in enumerate(events, start=1):
        if event.run_id != run_id:
            raise InvalidAppend(f"event {event.event_id} belongs to run {event.run_id}")
        if event.sequence != expected_version + offset:
            raise InvalidAppend(
                f"event {event.event_id} has sequence {event.sequence}, "
                f"expected {expected_version + offset}"
            )


def duplicate_within(events: Sequence[EventEnvelope]) -> UUID | None:
    seen: set[UUID] = set()
    for event in events:
        if event.event_id in seen:
            return event.event_id
        seen.add(event.event_id)
    return None


async def load_run(store: RunStore, run_id: UUID) -> RunState:
    """Reconstruct a run's current state from its stored event log."""
    return fold(await store.read(run_id))
