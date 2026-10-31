"""The memory port: what the runtime needs to recall memories into a run's context (ADR-0020).

The memory system itself (records, write policy, lifecycle, storage, retrieval) lives in
`hollis.memory`. The runtime depends only on these types:

- `MemoryAccess`: the namespaces a run may read and write. Granted by the application in the
  `RunSpec`, recorded in `RunCreated`, and passed to tools in `ToolContext`. The model cannot
  widen it.
- `MemoryRecall`: retrieval for a query, restricted to the granted namespaces.
- `RecalledMemory`: what was recalled, with provenance, recorded in the run log as it was shown.

Recalled memories are data. They are rendered as context blocks, never as instructions, and no
authorization decision reads them.
"""

import re
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

SCOPE_PATTERN = r"^[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9._@/+:-]{1,128}$"
"""`<kind>:<id>`, e.g. `user:alice`, `agent:researcher`, `workspace:acme/billing`."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MemoryKind(StrEnum):
    FACT = "fact"
    """A statement about the world, the user, a project or a system."""
    PREFERENCE = "preference"
    """How someone wants things done, including constraints they set."""
    PROCEDURE = "procedure"
    """How to do something; what worked or failed before."""
    EPISODE = "episode"
    """What happened in a past run: task, actions, outcome."""
    DECISION = "decision"
    """A choice that was made, with its rationale."""
    OBSERVATION = "observation"
    """Something seen during execution, not (yet) established as fact."""


class Certainty(StrEnum):
    """Epistemic status of a memory. Ordered weakest first."""

    INFERRED = "inferred"
    """Concluded by a model or extractor; may be wrong."""
    OBSERVED = "observed"
    """Seen directly in a tool result or event, not confirmed by a person."""
    CONFIRMED = "confirmed"
    """Stated or confirmed by a person or the operator."""

    @property
    def rank(self) -> int:
        return list(Certainty).index(self)


class MemoryAccess(_Frozen):
    """Namespaces a run may use. Every write namespace must also be readable."""

    read: tuple[str, ...] = Field(default=(), max_length=32)
    write: tuple[str, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def _check(self) -> "MemoryAccess":
        for scope in (*self.read, *self.write):
            if not re.fullmatch(SCOPE_PATTERN, scope):
                raise ValueError(f"invalid memory scope {scope!r} (expected kind:id)")
        missing = set(self.write) - set(self.read)
        if missing:
            raise ValueError(f"write scopes must also be readable: {sorted(missing)}")
        return self

    def can_read(self, scope: str) -> bool:
        return scope in self.read

    def can_write(self, scope: str) -> bool:
        return scope in self.write


class RecalledMemory(_Frozen):
    memory_id: str
    scope: str
    kind: MemoryKind
    content: str
    certainty: Certainty
    subject: str | None = None
    recorded_at: AwareDatetime
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    source: str
    """Provenance label, e.g. `user`, `run:<id>/turn/2/call/0`, `tool:fs_read_file`."""
    external: bool = False
    """Derived from an external (untrusted) source such as an MCP tool result."""
    conflicts_with: tuple[str, ...] = ()
    """Active memories that contradict this one; both are shown, neither silently wins."""
    supersedes: str | None = None
    score: float = 0.0

    def render(self) -> str:
        """The text shown to a model: the content with its epistemic status and provenance."""
        facts = [self.kind.value, self.certainty.value, f"recorded {self.recorded_at:%Y-%m-%d}"]
        if self.valid_until is not None:
            facts.append(f"valid until {self.valid_until:%Y-%m-%d %H:%M}Z")
        if self.subject:
            facts.append(f"about {self.subject}")
        facts.append(f"source {self.source}")
        if self.external:
            facts.append("from an external source")
        if self.conflicts_with:
            facts.append("CONFLICTS with " + ", ".join(self.conflicts_with))
        return f"[{'; '.join(facts)}] {self.content}"


class RecallQuery(_Frozen):
    text: str
    scopes: tuple[str, ...]
    """Namespaces to search; the caller restricts these to the run's grant."""
    limit: int = Field(default=10, ge=1, le=100)
    kinds: tuple[MemoryKind, ...] = ()
    subject: str | None = None
    as_of: datetime | None = None
    """Return what was valid at this time (default: now)."""
    max_chars: int | None = Field(default=None, ge=1)
    """Stop adding memories once their rendered text would exceed this."""


class MemoryRecall(Protocol):
    async def recall(self, query: RecallQuery) -> tuple[RecalledMemory, ...]: ...
