"""Crash injection for recovery tests.

`SimulatedCrash` derives from BaseException so runtime code cannot accidentally swallow it,
mirroring a process dying at that point. The wrapped store and provider survive the "crash":
the store stands for durable storage and the provider for external, provider-side state.
"""

from collections.abc import Sequence
from uuid import UUID

from hollis.runtime.events import EventEnvelope, EventType
from hollis.runtime.model import ModelProvider, ModelRequest, ModelResponse, ProviderCapabilities
from hollis.runtime.store import RunStore


class SimulatedCrash(BaseException):
    pass


class CrashingStore:
    """Raises SimulatedCrash once, immediately after an append containing `crash_after` commits."""

    def __init__(self, inner: RunStore, crash_after: EventType | None = None) -> None:
        self._inner = inner
        self._crash_after = crash_after
        self.run_id: UUID | None = None

    async def append(
        self, run_id: UUID, expected_version: int, events: Sequence[EventEnvelope]
    ) -> None:
        await self._inner.append(run_id, expected_version, events)
        self.run_id = run_id
        if self._crash_after is not None and any(e.event_type == self._crash_after for e in events):
            self._crash_after = None
            raise SimulatedCrash(f"after {events[-1].event_type} committed")

    async def read(self, run_id: UUID) -> list[EventEnvelope]:
        return await self._inner.read(run_id)


class CrashAfterGenerate:
    """The provider produces a response, then the process dies before the runtime records it."""

    def __init__(self, inner: ModelProvider) -> None:
        self._inner = inner

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._inner.capabilities

    async def generate(self, request: ModelRequest) -> ModelResponse:
        await self._inner.generate(request)
        raise SimulatedCrash("after provider generated a response")
