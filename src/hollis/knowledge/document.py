"""The traceable representation of an extracted document (ADR-0023).

Extraction turns raw bytes into typed blocks with locations. Every later step (chunks, citations,
navigation) refers back to these locations, so an answer can point at "runbook.md, section
Recovery > GPU jobs, lines 40-52" rather than at anonymous text.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST = "list"
    CODE = "code"
    TABLE = "table"
    QUOTE = "quote"


class Location(_Frozen):
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)

    def label(self) -> str:
        parts: list[str] = []
        if self.page_start is not None:
            end = self.page_end or self.page_start
            parts.append(
                f"p. {self.page_start}"
                if end == self.page_start
                else f"pp. {self.page_start}-{end}"
            )
        if self.line_start is not None:
            end = self.line_end or self.line_start
            parts.append(
                f"line {self.line_start}"
                if end == self.line_start
                else f"lines {self.line_start}-{end}"
            )
        return ", ".join(parts)

    @staticmethod
    def span(locations: "list[Location]") -> "Location":
        def lo(values: list[int | None]) -> int | None:
            present = [v for v in values if v is not None]
            return min(present) if present else None

        def hi(values: list[int | None]) -> int | None:
            present = [v for v in values if v is not None]
            return max(present) if present else None

        return Location(
            page_start=lo([loc.page_start for loc in locations]),
            page_end=hi([loc.page_end or loc.page_start for loc in locations]),
            line_start=lo([loc.line_start for loc in locations]),
            line_end=hi([loc.line_end or loc.line_start for loc in locations]),
        )


class Block(_Frozen):
    kind: BlockKind
    text: str
    level: int = Field(default=0, ge=0, le=9)
    """Heading level (1 = top)."""
    location: Location = Location()
    language: str | None = None
    """For code blocks."""


class ExtractedDocument(_Frozen):
    media_type: str
    extractor: str
    """Extractor name and version; part of the source's index fingerprint."""
    title: str | None = None
    blocks: tuple[Block, ...] = ()
    metadata: dict[str, str] = Field(default_factory=dict[str, str])
    """Extraction facts (pages, removed hidden or invisible content). Never used for identity or
    access control."""
