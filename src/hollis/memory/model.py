"""Memory records, candidates and the outcomes of writing them (ADR-0020).

A memory is a typed, attributed statement with an epistemic status and a validity interval:

- `kind` says what sort of thing it is (fact, preference, procedure, episode, decision,
  observation); `certainty` says how much to trust it (inferred < observed < confirmed).
- `subject` + `key` name a *slot* ("user" / "units", "service:billing" / "region"). Slots are
  how Hollis tells a new fact from an update, a correction or a contradiction without guessing
  from wording.
- `valid_from` / `valid_until` say when the statement holds; `recorded_at` says when Hollis
  learned it. Queries "as of" a time see what was valid and known then.
- `status` records the lifecycle: active, superseded (true until replaced), retracted (was
  wrong, or withdrawn), expired (a temporary condition ended).
- `provenance` says where it came from, including whether it derives from untrusted content.
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from hollis.runtime.memory import SCOPE_PATTERN, Certainty, MemoryKind

MAX_CONTENT_CHARS = 4000


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    """Replaced by an update; it was true until `valid_until`."""
    RETRACTED = "retracted"
    """Corrected (it was wrong) or withdrawn; never recalled, kept for history."""
    EXPIRED = "expired"
    """A temporary condition whose validity ended."""


class ChangeKind(StrEnum):
    """What the writer asserts about a statement relative to its slot's current value."""

    NEW = "new"
    """An independent statement. If its slot already holds a different value, it is recorded as
    a conflict rather than silently replacing it."""
    UPDATE = "update"
    """The slot's value changed: the old value was true until now and is superseded."""
    CORRECTION = "correction"
    """The old value was wrong: it is retracted."""
    TEMPORARY = "temporary"
    """Holds until `valid_until`, then the slot's existing value applies again."""


class SourceKind(StrEnum):
    USER = "user"
    """A person, through the application."""
    OPERATOR = "operator"
    AGENT = "agent"
    """A model, through a memory tool."""
    TOOL = "tool"
    SYSTEM = "system"
    """Hollis itself (episodes) or an extractor."""


class Provenance(_Frozen):
    kind: SourceKind
    actor: str | None = None
    run_id: UUID | None = None
    reference: str | None = None
    """An invocation id, turn or event the memory was derived from."""
    external: bool = False
    """Derived from external (untrusted) content, such as an MCP tool result."""

    def label(self) -> str:
        parts = [self.kind.value]
        if self.actor:
            parts.append(self.actor)
        if self.reference:
            parts.append(self.reference)
        elif self.run_id:
            parts.append(f"run:{self.run_id}")
        return ":".join(parts)


class MemoryRecord(_Frozen):
    memory_id: str
    scope: str = Field(pattern=SCOPE_PATTERN)
    kind: MemoryKind
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    subject: str | None = None
    key: str | None = None
    certainty: Certainty
    status: MemoryStatus = MemoryStatus.ACTIVE
    status_reason: str | None = None
    provenance: Provenance
    recorded_at: AwareDatetime
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    supersedes: str | None = None
    superseded_by: str | None = None
    conflicts_with: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    content_hash: str
    dedup_key: str | None = None
    reinforced: int = 0
    """Times the same statement was asserted again."""
    recall_count: int = 0
    last_recalled_at: AwareDatetime | None = None

    def valid_at(self, at: datetime) -> bool:
        """Whether the statement held and was known at `at` (retracted ones never were)."""
        if self.status is MemoryStatus.RETRACTED or self.recorded_at > at:
            return False
        if self.valid_from is not None and self.valid_from > at:
            return False
        return self.valid_until is None or self.valid_until > at


class MemoryCandidate(_Frozen):
    """A proposed memory. Nothing is stored until the write policy and lifecycle accept it."""

    scope: str = Field(pattern=SCOPE_PATTERN)
    kind: MemoryKind
    content: str = Field(min_length=1, max_length=MAX_CONTENT_CHARS)
    subject: str | None = Field(default=None, max_length=200)
    key: str | None = Field(default=None, max_length=200)
    certainty: Certainty = Certainty.INFERRED
    """Requested; the policy caps it by source (a model cannot confirm its own inferences)."""
    change: ChangeKind = ChangeKind.NEW
    provenance: Provenance
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    tags: tuple[str, ...] = Field(default=(), max_length=16)
    dedup_key: str | None = Field(default=None, max_length=300)
    """Idempotency key: a second write with the same key in the same scope is a no-op."""

    @model_validator(mode="after")
    def _check(self) -> "MemoryCandidate":
        if self.change is not ChangeKind.NEW and (not self.subject or not self.key):
            raise ValueError(f"a {self.change.value} needs a subject and key (the slot it changes)")
        if self.change is ChangeKind.TEMPORARY and self.valid_until is None:
            raise ValueError("a temporary memory needs valid_until")
        if (
            self.valid_from is not None
            and self.valid_until is not None
            and self.valid_until <= self.valid_from
        ):
            raise ValueError("valid_until must be after valid_from")
        return self


class WriteAction(StrEnum):
    CREATED = "created"
    REINFORCED = "reinforced"
    """The same statement already existed; it was reinforced (and possibly upgraded in
    certainty) instead of duplicated."""
    SUPERSEDED = "superseded"
    """Created; the slot's previous value is superseded (an update)."""
    CORRECTED = "corrected"
    """Created; the slot's previous value is retracted (a correction)."""
    CONFLICT = "conflict"
    """Created and linked to the active value(s) it contradicts; nothing was replaced."""
    DUPLICATE = "duplicate"
    """A write with the same dedup key already happened; nothing changed."""
    REJECTED = "rejected"


class WriteOutcome(_Frozen):
    action: WriteAction
    record: MemoryRecord | None = None
    affected: tuple[str, ...] = ()
    """Other memories changed or linked by this write."""
    reason: str = ""


class ChangeAction(StrEnum):
    CREATED = "created"
    REINFORCED = "reinforced"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    EXPIRED = "expired"
    CONFLICT_LINKED = "conflict_linked"
    DELETED = "deleted"


class MemoryChange(_Frozen):
    """One entry in the append-only change log. A deletion leaves a tombstone without content."""

    memory_id: str
    scope: str
    at: AwareDatetime
    action: ChangeAction
    actor: str
    detail: str = ""
