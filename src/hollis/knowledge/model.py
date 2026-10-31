"""Knowledge records: collections, sources, sections and chunks (ADR-0023, ADR-0024).

- A *collection* is a named set of sources with one embedding space (or none).
- A *source* is one external item (a file, a page, a record), identified within its collection by
  a caller-chosen stable `key` (e.g. a relative path or URL). Its `fingerprint` is the hash of the
  raw content; its `index_fingerprint` also covers the extractor, chunking and analyzer versions,
  so changing any of them re-indexes. `version` increases on every content change.
- *Sections* mirror the document's heading tree; *chunks* are the retrievable units, each with its
  section, location and the source version it came from.
- `labels` (access control), `trust` and `effective_date` are set by the operator at ingestion and
  can be changed later; nothing in a document can set them.
"""

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from hollis.knowledge.document import BlockKind, Location
from hollis.runtime.embedding import EmbeddingSpace
from hollis.runtime.knowledge import COLLECTION_PATTERN, SourceTrust


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Collection(_Frozen):
    name: str = Field(pattern=COLLECTION_PATTERN)
    description: str = ""
    space: EmbeddingSpace | None = None
    """The collection's embedding space; None for lexical-only collections."""
    created_at: AwareDatetime


class Source(_Frozen):
    source_id: str
    collection: str
    key: str = Field(min_length=1, max_length=1024)
    uri: str | None = None
    """Where it came from, for operators. Not shown to models (paths can be sensitive)."""
    title: str
    media_type: str
    fingerprint: str
    index_fingerprint: str
    version: int = Field(ge=1)
    labels: tuple[str, ...] = ()
    trust: SourceTrust
    effective_date: AwareDatetime | None = None
    ingested_at: AwareDatetime
    """When this version was indexed."""
    size_bytes: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    extraction: dict[str, str] = Field(default_factory=dict[str, str])


class Section(_Frozen):
    section_id: str
    source_id: str
    index: int
    title: str
    level: int
    parent: int | None
    path: tuple[str, ...]
    location: Location = Location()


class Chunk(_Frozen):
    chunk_id: str
    source_id: str
    collection: str
    source_version: int
    section_id: str
    ordinal: int
    text: str
    path: tuple[str, ...]
    kinds: tuple[BlockKind, ...]
    location: Location
    flags: tuple[str, ...] = ()
    tokens: int
    content_hash: str

    def location_label(self) -> str:
        parts = [" > ".join(self.path)] if self.path else []
        if label := self.location.label():
            parts.append(label)
        return ", ".join(parts)
