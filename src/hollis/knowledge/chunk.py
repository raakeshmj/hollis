"""Structure-aware chunking (ADR-0023).

Chunks follow the document's structure instead of fixed character windows:
- every heading starts a section; sections form a tree (the parent of a chunk is its section);
- chunks are cut between blocks, never inside one, until a block alone exceeds the size limit;
- oversized code is split at line boundaries (blank lines preferred), oversized tables by rows
  with the header repeated in every part, oversized prose at sentence boundaries with a small
  overlap so a fact straddling the cut stays findable;
- each chunk keeps its section path and source location, and is flagged when it contains
  instruction-like text (see sanitize).

Sizes use the runtime's conservative token estimate, so chunks fit context budgets predictably.
"""

import re

from pydantic import BaseModel, ConfigDict, Field

from hollis.knowledge.document import Block, BlockKind, ExtractedDocument, Location
from hollis.knowledge.sanitize import instruction_flags
from hollis.runtime.context import estimate_text


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ChunkingPolicy(_Frozen):
    target_tokens: int = Field(default=300, ge=32, le=4000)
    max_tokens: int = Field(default=500, ge=64, le=8000)
    overlap_tokens: int = Field(default=40, ge=0, le=500)
    version: str = "1"
    """Part of the index fingerprint: changing the policy re-indexes sources."""


class SectionDraft(_Frozen):
    index: int
    title: str
    level: int
    parent: int | None
    path: tuple[str, ...]
    location: Location = Location()


class ChunkDraft(_Frozen):
    ordinal: int
    section: int
    text: str
    kinds: tuple[BlockKind, ...]
    location: Location
    flags: tuple[str, ...] = ()
    tokens: int


class ChunkedDocument(_Frozen):
    sections: tuple[SectionDraft, ...]
    chunks: tuple[ChunkDraft, ...]


_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


def chunk(document: ExtractedDocument, policy: ChunkingPolicy | None = None) -> ChunkedDocument:
    policy = policy or ChunkingPolicy()
    sections = [SectionDraft(index=0, title=document.title or "", level=0, parent=None, path=())]
    stack: list[SectionDraft] = [sections[0]]
    chunks: list[ChunkDraft] = []
    buffer: list[Block] = []
    used = 0

    def flush() -> None:
        nonlocal buffer, used
        if buffer:
            text = "\n\n".join(b.text for b in buffer)
            chunks.append(
                ChunkDraft(
                    ordinal=len(chunks),
                    section=stack[-1].index,
                    text=text,
                    kinds=tuple(dict.fromkeys(b.kind for b in buffer)),
                    location=Location.span([b.location for b in buffer]),
                    flags=instruction_flags(text),
                    tokens=estimate_text(text),
                )
            )
        buffer, used = [], 0

    for block in document.blocks:
        if block.kind is BlockKind.HEADING:
            flush()
            level = max(block.level, 1)
            while len(stack) > 1 and stack[-1].level >= level:
                stack.pop()
            parent = stack[-1]
            section = SectionDraft(
                index=len(sections),
                title=block.text,
                level=level,
                parent=parent.index,
                path=(*parent.path, block.text),
                location=block.location,
            )
            sections.append(section)
            stack.append(section)
            continue
        for piece in _split(block, policy):
            size = estimate_text(piece.text)
            if buffer and used + size > policy.target_tokens:
                flush()
            buffer.append(piece)
            used += size
    flush()
    return ChunkedDocument(sections=tuple(sections), chunks=tuple(chunks))


def _split(block: Block, policy: ChunkingPolicy) -> list[Block]:
    if estimate_text(block.text) <= policy.max_tokens:
        return [block]
    if block.kind is BlockKind.CODE:
        return _split_lines(block, policy)
    if block.kind is BlockKind.TABLE:
        return _split_table(block, policy)
    return _split_sentences(block, policy)


def _split_lines(block: Block, policy: ChunkingPolicy) -> list[Block]:
    lines = block.text.split("\n")
    start_line = block.location.line_start
    pieces: list[Block] = []
    current: list[str] = []
    first = 0
    for line in lines:
        candidate = [*current, line]
        if current and estimate_text("\n".join(candidate)) > policy.max_tokens:
            cut = len(current)
            blanks = [i for i, text in enumerate(current) if not text.strip()]
            if blanks and blanks[-1] > len(current) // 2:
                cut = blanks[-1]
            pieces.append(_piece(block, current[:cut], start_line, first))
            first += cut
            current = current[cut:]
        current.append(line)
    if current:
        pieces.append(_piece(block, current, start_line, first))
    return pieces


def _piece(block: Block, lines: list[str], start_line: int | None, offset: int) -> Block:
    location = block.location
    if start_line is not None:
        location = Location(
            page_start=location.page_start,
            page_end=location.page_end,
            line_start=start_line + offset,
            line_end=start_line + offset + len(lines) - 1,
        )
    return Block(kind=block.kind, text="\n".join(lines), location=location, language=block.language)


def _split_table(block: Block, policy: ChunkingPolicy) -> list[Block]:
    rows = block.text.split("\n")
    header = (
        rows[:2]
        if len(rows) > 1 and set(rows[1].replace("|", "").strip()) <= set("-: ")
        else rows[:1]
    )
    body = rows[len(header) :]
    pieces: list[Block] = []
    current: list[str] = []
    for row in body:
        if current and estimate_text("\n".join([*header, *current, row])) > policy.max_tokens:
            pieces.append(
                Block(kind=block.kind, text="\n".join([*header, *current]), location=block.location)
            )
            current = []
        current.append(row)
    if current or not pieces:
        pieces.append(
            Block(kind=block.kind, text="\n".join([*header, *current]), location=block.location)
        )
    return pieces


def _split_sentences(block: Block, policy: ChunkingPolicy) -> list[Block]:
    sentences = [s for s in _SENTENCE.split(block.text) if s.strip()]
    pieces: list[Block] = []
    current: list[str] = []
    for sentence in sentences:
        if current and estimate_text(" ".join([*current, sentence])) > policy.max_tokens:
            pieces.append(Block(kind=block.kind, text=" ".join(current), location=block.location))
            overlap: list[str] = []
            for previous in reversed(current):
                if estimate_text(" ".join([previous, *overlap])) > policy.overlap_tokens:
                    break
                overlap.insert(0, previous)
            current = overlap
        current.append(sentence)
    if current:
        pieces.append(Block(kind=block.kind, text=" ".join(current), location=block.location))
    return pieces
