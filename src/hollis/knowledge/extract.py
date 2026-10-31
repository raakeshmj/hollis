"""Content extraction: raw bytes to a structured, located document (ADR-0023).

Formats are detected from content first (PDF and Office signatures, binary data) and only then
from the file name, so a renamed file is not parsed with the wrong extractor. Each extractor
returns typed blocks with page or line locations:

- Markdown: ATX and setext headings, fenced code, pipe tables, lists, quotes, paragraphs.
  Front matter is read for the title only; it can never set identity or access control.
- Plain text: paragraphs with line ranges.
- HTML (stdlib parser): headings, paragraphs, lists, code, tables. Scripts, styles, templates,
  comments and *hidden* elements (`hidden`, `aria-hidden`, `display:none`, `visibility:hidden`,
  zero font size or opacity) are dropped and counted: content a human would not see must not
  reach a model.
- Source code: Python split at top-level definitions (`ast`), other languages at definition-like
  lines, with line ranges.
- JSON: top-level keys as sections, flattened `path: value` lines. CSV: tables with the header
  repeated in every block.
- DOCX (stdlib zip + XML): heading styles, paragraphs, tables. Size-limited against zip bombs.
- PDF: text per page with page numbers, through the optional `pypdf` extra. Scanned PDFs (no text
  layer) yield no text; OCR is not part of M5.

Extractors are pluggable: `register(media_type, extractor)`.
"""

import ast
import csv
import io
import json
import re
import zipfile
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import PurePath
from typing import Any, cast
from xml.etree import ElementTree

from hollis.knowledge.document import Block, BlockKind, ExtractedDocument, Location
from hollis.knowledge.sanitize import sanitize

EXTRACTOR_VERSION = "1"
MAX_DOCX_XML_BYTES = 50_000_000


class UnsupportedFormat(ValueError):
    pass


type Extractor = Callable[[bytes, str], ExtractedDocument]

CODE_LANGUAGES = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp",
    ".hpp": "cpp", ".cs": "csharp", ".swift": "swift", ".scala": "scala", ".sh": "shell",
    ".bash": "shell", ".sql": "sql", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".ini": "ini", ".tf": "terraform", ".proto": "protobuf",
}  # fmt: skip
TEXT_TYPES = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".mdx": "text/markdown",
    ".html": "text/html", ".htm": "text/html", ".json": "application/json", ".csv": "text/csv",
    ".txt": "text/plain", ".rst": "text/plain", ".log": "text/plain",
}  # fmt: skip


def detect(data: bytes, name: str) -> str:
    """The media type, from content signatures first and the name second."""
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if "word/document.xml" in archive.namelist():
                    return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        except zipfile.BadZipFile:
            pass
        raise UnsupportedFormat(f"{name}: unsupported archive format")
    if b"\x00" in data[:8192]:
        raise UnsupportedFormat(f"{name}: binary content is not supported")
    suffix = PurePath(name).suffix.lower()
    if suffix in CODE_LANGUAGES:
        return f"text/x-code;language={CODE_LANGUAGES[suffix]}"
    if suffix in TEXT_TYPES:
        return TEXT_TYPES[suffix]
    head = data[:512].lstrip().lower()
    if head.startswith((b"<!doctype html", b"<html")):
        return "text/html"
    return "text/plain"


def _text(data: bytes, name: str) -> tuple[str, int]:
    try:
        decoded = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UnsupportedFormat(f"{name}: text is not valid UTF-8") from exc
    return sanitize(decoded)


def _lines_location(start: int, end: int) -> Location:
    return Location(line_start=start, line_end=end)


# --- markdown and text ------------------------------------------------------------------------

_ATX = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^(```|~~~)\s*([\w+-]*)")
_LIST = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def extract_markdown(data: bytes, name: str) -> ExtractedDocument:
    text, removed = _text(data, name)
    lines = text.split("\n")
    blocks: list[Block] = []
    title: str | None = None
    i = 0
    if lines and lines[0].strip() == "---":  # front matter: title only, never identity or ACL
        for j in range(1, min(len(lines), 200)):
            if lines[j].strip() == "---":
                for meta in lines[1:j]:
                    if meta.lower().startswith("title:"):
                        title = meta.split(":", 1)[1].strip().strip("'\"") or None
                i = j + 1
                break
    paragraph: list[str] = []
    para_start = 0

    def flush(kind: BlockKind = BlockKind.PARAGRAPH) -> None:
        nonlocal paragraph
        if paragraph:
            content = "\n".join(paragraph).strip()
            if content:
                blocks.append(
                    Block(
                        kind=kind,
                        text=content,
                        location=_lines_location(para_start, para_start + len(paragraph) - 1),
                    )
                )
        paragraph = []

    current_kind = BlockKind.PARAGRAPH
    while i < len(lines):
        line = lines[i]
        number = i + 1
        fence = _FENCE.match(line)
        if fence:
            flush(current_kind)
            marker, language = fence.group(1), fence.group(2) or None
            body: list[str] = []
            j = i + 1
            while j < len(lines) and not lines[j].startswith(marker):
                body.append(lines[j])
                j += 1
            blocks.append(
                Block(
                    kind=BlockKind.CODE,
                    text="\n".join(body),
                    language=language,
                    location=_lines_location(number, min(j + 1, len(lines))),
                )
            )
            i = j + 1
            continue
        heading = _ATX.match(line)
        setext = (
            i + 1 < len(lines)
            and line.strip()
            and re.match(r"^(=+|-+)\s*$", lines[i + 1])
            and not paragraph
        )
        if heading or setext:
            flush(current_kind)
            if heading:
                level, content = len(heading.group(1)), heading.group(2)
                step = 1
            else:
                level, content = (1 if lines[i + 1].startswith("=") else 2), line.strip()
                step = 2
            blocks.append(
                Block(
                    kind=BlockKind.HEADING,
                    text=content,
                    level=level,
                    location=_lines_location(number, number),
                )
            )
            title = title or (content if level == 1 else None)
            i += step
            continue
        if line.lstrip().startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            flush(current_kind)
            rows = [line, lines[i + 1]]  # header and separator
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                rows.append(lines[j])
                j += 1
            blocks.append(
                Block(
                    kind=BlockKind.TABLE,
                    text="\n".join(r.strip() for r in rows),
                    location=_lines_location(number, j),
                )
            )
            i = j
            continue
        if not line.strip():
            flush(current_kind)
            current_kind = BlockKind.PARAGRAPH
            i += 1
            continue
        kind = (
            BlockKind.LIST
            if _LIST.match(line)
            else BlockKind.QUOTE
            if line.lstrip().startswith(">")
            else BlockKind.PARAGRAPH
        )
        if paragraph and kind is not current_kind and kind is not BlockKind.PARAGRAPH:
            flush(current_kind)
        if not paragraph:
            para_start = number
            current_kind = kind
        paragraph.append(line)
        i += 1
    flush(current_kind)
    return ExtractedDocument(
        media_type="text/markdown",
        extractor=f"markdown/{EXTRACTOR_VERSION}",
        title=title,
        blocks=tuple(blocks),
        metadata={"removed_invisible": str(removed)},
    )


def extract_text(data: bytes, name: str) -> ExtractedDocument:
    text, removed = _text(data, name)
    blocks: list[Block] = []
    start: int | None = None
    buffer: list[str] = []
    for number, line in enumerate([*text.split("\n"), ""], start=1):
        if line.strip():
            if start is None:
                start = number
            buffer.append(line)
        elif buffer and start is not None:
            blocks.append(
                Block(
                    kind=BlockKind.PARAGRAPH,
                    text="\n".join(buffer),
                    location=_lines_location(start, number - 1),
                )
            )
            buffer, start = [], None
    return ExtractedDocument(
        media_type="text/plain",
        extractor=f"text/{EXTRACTOR_VERSION}",
        blocks=tuple(blocks),
        metadata={"removed_invisible": str(removed)},
    )


# --- HTML -------------------------------------------------------------------------------------

_DROP = {
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "iframe",
    "object",
    "embed",
    "head",
    "button",
    "form",
}
_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "track",
    "wbr",
}
_HIDDEN_STYLE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d]*[1-9])|opacity\s*:\s*0(?![.\d]*[1-9])",
    re.I,
)


class _HTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.title: str | None = None
        self.hidden_removed = 0
        self._skip = 0  # depth inside dropped or hidden elements
        self._stack: list[str] = []
        self._buffer: list[str] = []
        self._kind = BlockKind.PARAGRAPH
        self._level = 0
        self._in_title = False
        self._row: list[str] = []
        self._rows: list[str] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _VOID:
            if tag == "br" and not self._skip:
                self._buffer.append("\n")
            return
        if tag == "title":
            self._in_title = True  # the page title is kept even inside <head>
            return
        attributes = {k: (v or "") for k, v in attrs}
        hidden = (
            "hidden" in attributes
            or attributes.get("aria-hidden", "").lower() == "true"
            or bool(_HIDDEN_STYLE.search(attributes.get("style", "")))
        )
        if self._skip or tag in _DROP or hidden:
            if hidden and not self._skip:
                self.hidden_removed += 1
            self._skip += 1
            self._stack.append(tag)
            return
        self._stack.append(tag)
        if re.fullmatch(r"h[1-6]", tag):
            self._flush()
            self._kind, self._level = BlockKind.HEADING, int(tag[1])
        elif tag in (
            "p",
            "div",
            "section",
            "article",
            "main",
            "blockquote",
            "li",
            "pre",
            "dd",
            "dt",
        ):
            self._flush()
            self._kind = {
                "li": BlockKind.LIST,
                "pre": BlockKind.CODE,
                "blockquote": BlockKind.QUOTE,
            }.get(tag, BlockKind.PARAGRAPH)
        elif tag == "table":
            self._flush()
            self._rows = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "tr":
            self._row = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        if tag == "title" and self._in_title:
            self._in_title = False
            return
        if tag not in self._stack:
            return  # stray end tag
        while self._stack:
            open_tag = self._stack.pop()
            if self._skip:
                self._skip -= 1
                if open_tag == tag:
                    return
                continue
            if open_tag == tag:
                break
        if self._skip:
            return
        if re.fullmatch(r"h[1-6]", tag) or tag in (
            "p",
            "div",
            "section",
            "article",
            "main",
            "blockquote",
            "li",
            "pre",
            "dd",
            "dt",
        ):
            self._flush()
        elif tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row:
            self._rows.append("| " + " | ".join(self._row) + " |")
            self._row = []
        elif tag == "table" and self._rows:
            self.blocks.append(Block(kind=BlockKind.TABLE, text="\n".join(self._rows)))
            self._rows = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title = ((self.title or "") + data).strip() or None
            return
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
        else:
            self._buffer.append(data)

    def handle_comment(self, data: str) -> None:
        return  # comments never reach the model

    def _flush(self) -> None:
        raw = "".join(self._buffer)
        self._buffer = []
        text = raw.strip("\n") if self._kind is BlockKind.CODE else " ".join(raw.split())
        if text:
            self.blocks.append(
                Block(
                    kind=self._kind,
                    text=text,
                    level=self._level if self._kind is BlockKind.HEADING else 0,
                )
            )
        self._kind, self._level = BlockKind.PARAGRAPH, 0

    def close(self) -> None:
        super().close()
        self._flush()


def extract_html(data: bytes, name: str) -> ExtractedDocument:
    text, removed = _text(data, name)
    parser = _HTML()
    parser.feed(text)
    parser.close()
    return ExtractedDocument(
        media_type="text/html",
        extractor=f"html/{EXTRACTOR_VERSION}",
        title=parser.title
        or next((b.text for b in parser.blocks if b.kind is BlockKind.HEADING), None),
        blocks=tuple(parser.blocks),
        metadata={
            "removed_invisible": str(removed),
            "removed_hidden_elements": str(parser.hidden_removed),
        },
    )


# --- code -------------------------------------------------------------------------------------

_DEFINITION = re.compile(
    r"^(export\s+)?(async\s+)?(def|class|function|func|fn|struct|enum|interface|trait|impl|type|module|"
    r"public|private|protected|static|resource|message|service)\b"
)


def extract_code(data: bytes, name: str, language: str) -> ExtractedDocument:
    text, removed = _text(data, name)
    lines = text.split("\n")
    starts: list[tuple[int, str]] = []
    if language == "python":
        try:
            tree = ast.parse(text)
            docstring = ast.get_docstring(tree)
            for node in tree.body:
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                    first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                    kind = "class" if isinstance(node, ast.ClassDef) else "def"
                    starts.append((first, f"{kind} {node.name}"))
        except SyntaxError:
            docstring = None
    else:
        docstring = None
        starts = [
            (n, line.strip()[:80])
            for n, line in enumerate(lines, start=1)
            if _DEFINITION.match(line)
        ]
    blocks: list[Block] = []
    if docstring:
        blocks.append(
            Block(kind=BlockKind.PARAGRAPH, text=docstring, location=_lines_location(1, 1))
        )
    boundaries = [1, *(s for s, _ in starts if s > 1)]
    names = {s: label for s, label in starts}
    for index, start in enumerate(boundaries):
        end = boundaries[index + 1] - 1 if index + 1 < len(boundaries) else len(lines)
        body = "\n".join(lines[start - 1 : end]).strip("\n")
        if not body.strip():
            continue
        if start in names:
            blocks.append(
                Block(
                    kind=BlockKind.HEADING,
                    text=names[start],
                    level=2,
                    location=_lines_location(start, start),
                )
            )
        blocks.append(
            Block(
                kind=BlockKind.CODE,
                text=body,
                language=language,
                location=_lines_location(start, end),
            )
        )
    return ExtractedDocument(
        media_type=f"text/x-code;language={language}",
        extractor=f"code/{EXTRACTOR_VERSION}",
        title=PurePath(name).name,
        blocks=tuple(blocks),
        metadata={"removed_invisible": str(removed), "language": language},
    )


# --- structured data --------------------------------------------------------------------------

CSV_ROWS_PER_BLOCK = 25


def extract_json(data: bytes, name: str) -> ExtractedDocument:
    text, removed = _text(data, name)
    try:
        value: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UnsupportedFormat(f"{name}: invalid JSON ({exc.msg})") from exc
    blocks: list[Block] = []

    def flatten(prefix: str, item: Any, out: list[str]) -> None:
        if isinstance(item, dict):
            for key, child in cast(dict[str, Any], item).items():
                flatten(f"{prefix}.{key}" if prefix else str(key), child, out)
        elif isinstance(item, list):
            for index, child in enumerate(cast(list[Any], item)):
                flatten(f"{prefix}[{index}]", child, out)
        else:
            out.append(f"{prefix}: {json.dumps(item, ensure_ascii=False)}")

    sections = cast(dict[str, Any], value).items() if isinstance(value, dict) else [("", value)]
    for key, child in sections:
        lines: list[str] = []
        flatten(str(key), child, lines)
        if key:
            blocks.append(Block(kind=BlockKind.HEADING, text=str(key), level=2))
        for start in range(0, len(lines), 40):
            blocks.append(
                Block(kind=BlockKind.PARAGRAPH, text="\n".join(lines[start : start + 40]))
            )
    return ExtractedDocument(
        media_type="application/json",
        extractor=f"json/{EXTRACTOR_VERSION}",
        title=PurePath(name).name,
        blocks=tuple(blocks),
        metadata={"removed_invisible": str(removed)},
    )


def extract_csv(data: bytes, name: str) -> ExtractedDocument:
    text, removed = _text(data, name)
    rows = list(csv.reader(io.StringIO(text)))
    blocks: list[Block] = []
    if rows:
        header, body = rows[0], rows[1:]
        head = "| " + " | ".join(header) + " |"
        for start in range(0, max(len(body), 1), CSV_ROWS_PER_BLOCK):
            chunk = body[start : start + CSV_ROWS_PER_BLOCK]
            table = [head, *("| " + " | ".join(r) + " |" for r in chunk)]
            blocks.append(
                Block(
                    kind=BlockKind.TABLE,
                    text="\n".join(table),
                    location=_lines_location(start + 2, start + 1 + max(len(chunk), 1)),
                )
            )
    return ExtractedDocument(
        media_type="text/csv",
        extractor=f"csv/{EXTRACTOR_VERSION}",
        title=PurePath(name).name,
        blocks=tuple(blocks),
        metadata={"removed_invisible": str(removed), "rows": str(max(len(rows) - 1, 0))},
    )


# --- office and PDF ---------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract_docx(data: bytes, name: str) -> ExtractedDocument:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        info = archive.getinfo("word/document.xml")
        if info.file_size > MAX_DOCX_XML_BYTES:
            raise UnsupportedFormat(
                f"{name}: document.xml is {info.file_size} bytes (limit {MAX_DOCX_XML_BYTES})"
            )
        xml = archive.read(info)
    if b"<!DOCTYPE" in xml[:2048] or b"<!ENTITY" in xml[:4096]:
        raise UnsupportedFormat(f"{name}: DOCTYPE and entity declarations are not allowed")
    root = ElementTree.fromstring(xml)  # noqa: S314  # DTDs refused above; expat >= 2.4 limits expansion
    blocks: list[Block] = []
    removed_total = 0
    title: str | None = None
    body = root.find(f"{_W}body")
    elements: list[ElementTree.Element] = list(body) if body is not None else []
    for element in elements:
        if element.tag == f"{_W}p":
            text, removed = sanitize("".join(t.text or "" for t in element.iter(f"{_W}t")))
            removed_total += removed
            if not text.strip():
                continue
            style = element.find(f"{_W}pPr/{_W}pStyle")
            value = style.get(f"{_W}val", "") if style is not None else ""
            match = re.fullmatch(r"(?i)heading\s*([1-6])", value)
            if value.lower() == "title" or match:
                level = int(match.group(1)) if match else 1
                blocks.append(Block(kind=BlockKind.HEADING, text=text.strip(), level=level))
                title = title or text.strip()
            else:
                kind = (
                    BlockKind.LIST
                    if element.find(f"{_W}pPr/{_W}numPr") is not None
                    else BlockKind.PARAGRAPH
                )
                blocks.append(Block(kind=kind, text=text))
        elif element.tag == f"{_W}tbl":
            rows: list[str] = []
            for row in element.iter(f"{_W}tr"):
                cells = [
                    " ".join("".join(t.text or "" for t in cell.iter(f"{_W}t")).split())
                    for cell in row.iter(f"{_W}tc")
                ]
                rows.append("| " + " | ".join(cells) + " |")
            text, removed = sanitize("\n".join(rows))
            removed_total += removed
            if text.strip():
                blocks.append(Block(kind=BlockKind.TABLE, text=text))
    return ExtractedDocument(
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        extractor=f"docx/{EXTRACTOR_VERSION}",
        title=title,
        blocks=tuple(blocks),
        metadata={"removed_invisible": str(removed_total)},
    )


def extract_pdf(data: bytes, name: str) -> ExtractedDocument:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise UnsupportedFormat(
            f"{name}: PDF support needs the optional extra: pip install 'hollis[pdf]'"
        ) from exc
    reader = PdfReader(io.BytesIO(data))
    blocks: list[Block] = []
    removed_total = 0
    empty_pages = 0
    for number, page in enumerate(reader.pages, start=1):
        text, removed = sanitize(page.extract_text() or "")
        removed_total += removed
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if not paragraphs:
            empty_pages += 1
        blocks.extend(
            Block(
                kind=BlockKind.PARAGRAPH,
                text=p,
                location=Location(page_start=number, page_end=number),
            )
            for p in paragraphs
        )
    title = reader.metadata.title if reader.metadata is not None and reader.metadata.title else None
    return ExtractedDocument(
        media_type="application/pdf",
        extractor=f"pdf-pypdf/{EXTRACTOR_VERSION}",
        title=str(title) if title else None,
        blocks=tuple(blocks),
        metadata={
            "pages": str(len(reader.pages)),
            "pages_without_text": str(empty_pages),
            "removed_invisible": str(removed_total),
        },
    )


_EXTRACTORS: dict[str, Extractor] = {
    "text/markdown": extract_markdown,
    "text/plain": extract_text,
    "text/html": extract_html,
    "application/json": extract_json,
    "text/csv": extract_csv,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_docx,
    "application/pdf": extract_pdf,
}


def register(media_type: str, extractor: Extractor) -> None:
    """Add or replace the extractor for a media type."""
    _EXTRACTORS[media_type] = extractor


def extract(data: bytes, name: str, media_type: str | None = None) -> ExtractedDocument:
    detected = media_type or detect(data, name)
    if detected.startswith("text/x-code;language="):
        return extract_code(data, name, detected.split("=", 1)[1])
    extractor = _EXTRACTORS.get(detected)
    if extractor is None:
        raise UnsupportedFormat(f"{name}: no extractor for {detected}")
    return extractor(data, name)
