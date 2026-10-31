from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hollis.adapters.sqlite_memory import SqliteMemoryStore
from hollis.memory import InMemoryMemoryStore, MemoryManager, MemoryStore
from hollis.runtime.memory import MemoryAccess

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
ALICE = MemoryAccess(read=("user:alice", "workspace:acme"), write=("user:alice",))
BOB = MemoryAccess(read=("user:bob", "workspace:acme"), write=("user:bob",))


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now


@pytest.fixture(params=["memory", "sqlite"])
async def memory_store(request: pytest.FixtureRequest, tmp_path: Path) -> MemoryStore:
    if request.param == "memory":
        return InMemoryMemoryStore()
    return await SqliteMemoryStore.open(tmp_path / "memory.db")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def manager(memory_store: MemoryStore, clock: Clock) -> MemoryManager:
    return MemoryManager(memory_store, clock=clock)


ManagerFactory = Callable[..., MemoryManager]
