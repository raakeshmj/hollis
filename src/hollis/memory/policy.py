"""Deterministic memory write and retention policy (ADR-0020, ADR-0021).

Writes are not automatic: something must propose a candidate (the application, a model through a
memory tool, an episode recorder or an extractor), and this policy decides whether and how it is
stored. Nothing here reads model output to grant itself trust: certainty is capped by the
*source*, which the calling code sets, not by what the content claims.
"""

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from hollis.memory.model import MemoryCandidate, SourceKind
from hollis.runtime.memory import Certainty, MemoryKind
from hollis.runtime.redaction import Redactor

DEFAULT_MAX_CERTAINTY: Mapping[SourceKind, Certainty] = {
    SourceKind.USER: Certainty.CONFIRMED,
    SourceKind.OPERATOR: Certainty.CONFIRMED,
    SourceKind.SYSTEM: Certainty.OBSERVED,
    SourceKind.TOOL: Certainty.OBSERVED,
    SourceKind.AGENT: Certainty.INFERRED,
}

DEFAULT_MAX_AGE_DAYS: Mapping[MemoryKind, int] = {
    MemoryKind.OBSERVATION: 30,
    MemoryKind.EPISODE: 180,
}


class MemoryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_content_chars: int = Field(default=2000, ge=1, le=4000)
    allowed_kinds: Mapping[SourceKind, frozenset[MemoryKind]] = Field(
        default_factory=lambda: {source: frozenset(MemoryKind) for source in SourceKind}
    )
    max_certainty: Mapping[SourceKind, Certainty] = Field(
        default_factory=lambda: dict(DEFAULT_MAX_CERTAINTY)
    )
    """Certainty a source can establish. A model's own writes stay `inferred`."""
    max_external_certainty: Certainty = Certainty.OBSERVED
    """Cap for anything derived from external (untrusted) content."""
    reject_credential_patterns: bool = True
    """Also reject content matching common credential shapes (registered secrets are always
    rejected). Best effort: arbitrary sensitive text cannot be detected reliably."""
    max_records_per_scope: int = Field(default=10_000, ge=1)
    max_age_days: Mapping[MemoryKind, int] = Field(
        default_factory=lambda: dict(DEFAULT_MAX_AGE_DAYS)
    )
    """Retention for memories that are not confirmed and have not been recalled within the
    period. Confirmed memories are never deleted by maintenance."""
    history_retention_days: int | None = Field(default=None, ge=1)
    """Delete superseded, retracted and expired memories older than this. None keeps history."""

    def check(self, candidate: MemoryCandidate, redactor: Redactor) -> MemoryCandidate | str:
        """The candidate with its certainty capped, or the reason it is rejected."""
        source = candidate.provenance.kind
        if candidate.kind not in self.allowed_kinds.get(source, frozenset()):
            return f"{source.value} sources may not write {candidate.kind.value} memories"
        if len(candidate.content) > self.max_content_chars:
            limit = self.max_content_chars
            return f"content is {len(candidate.content)} characters; the limit is {limit}"
        text = " ".join(filter(None, (candidate.content, candidate.subject, candidate.key)))
        if redactor.contains_secret(text):
            return "content contains a registered credential"
        if self.reject_credential_patterns and redactor.redact(text) != text:
            return "content looks like it contains a credential"
        cap = self.max_certainty.get(source, Certainty.INFERRED)
        if candidate.provenance.external and self.max_external_certainty.rank < cap.rank:
            cap = self.max_external_certainty
        if candidate.certainty.rank > cap.rank:
            return candidate.model_copy(update={"certainty": cap})
        return candidate
