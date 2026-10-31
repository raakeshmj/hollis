"""Persistent agent memory: typed records, governed writes, ranked recall (ADR-0020, ADR-0021).

    store = await SqliteMemoryStore.open("memory.db")      # hollis.adapters.sqlite_memory
    memory = MemoryManager(store, redactor=gateway_redactor)
    access = MemoryAccess(read=("user:alice", "workspace:acme"), write=("user:alice",))
    await memory.remember(user_fact("user:alice", "Prefers metric units",
                                    kind=MemoryKind.PREFERENCE, subject="user", key="units"),
                          access)
    runner = gateway.runner(run_store, memory=memory, tools=registry)   # recalls at run start
    await runner.create(RunSpec(..., memory=access))

This is agent memory, not document retrieval: M5 builds knowledge retrieval on the same storage
and provenance foundations.
"""

from hollis.memory.episodes import (
    ExtractionReport,
    MemoryExtractor,
    episode_candidate,
    record_episode,
)
from hollis.memory.manager import AccessDenied, MaintenanceReport, MemoryManager, user_fact
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
from hollis.memory.store import InMemoryMemoryStore, MemoryStore
from hollis.memory.tools import MEMORY_NOTICE, memory_tools
from hollis.runtime.memory import (
    Certainty,
    MemoryAccess,
    MemoryKind,
    RecalledMemory,
    RecallQuery,
)

__all__ = [
    "MEMORY_NOTICE",
    "AccessDenied",
    "Certainty",
    "ChangeAction",
    "ChangeKind",
    "ExtractionReport",
    "InMemoryMemoryStore",
    "MaintenanceReport",
    "MemoryAccess",
    "MemoryCandidate",
    "MemoryChange",
    "MemoryExtractor",
    "MemoryKind",
    "MemoryManager",
    "MemoryPolicy",
    "MemoryRecord",
    "MemoryStatus",
    "MemoryStore",
    "Provenance",
    "RecallQuery",
    "RecalledMemory",
    "SourceKind",
    "WriteAction",
    "WriteOutcome",
    "episode_candidate",
    "memory_tools",
    "record_episode",
    "user_fact",
]
