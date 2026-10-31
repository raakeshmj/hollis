"""Factories for every RunStore adapter, so contract and recovery tests run against all of them."""

from pathlib import Path

from hollis.adapters.memory_store import InMemoryRunStore
from hollis.runtime.store import RunStore

STORE_KINDS = ["memory"]


async def make_store(kind: str, directory: Path) -> RunStore:
    match kind:
        case "memory":
            return InMemoryRunStore()
        case _:
            raise ValueError(f"unknown store kind {kind!r}")
