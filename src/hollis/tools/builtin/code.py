"""Code tools confined to one root directory (M8): search, outline, numbered reads, patches.

- `code_search` finds text (or a guarded regular expression) in text files and answers with
  `path:line: text`, so results can be cited and opened precisely.
- `code_outline` lists a file's structure with line numbers: Python through `ast` (classes,
  functions, signatures, docstring summaries), other languages through simple patterns.
- `code_read` returns a numbered line range, for reading large files a piece at a time.
- `apply_patch` applies a unified diff to one or more files, all or nothing. Hunks are placed by
  their context, not by trusting their line numbers; an ambiguous or mismatching hunk refuses the
  whole patch. It can tell afterwards whether a patch is already applied (`reconcile`), so an
  interrupted call is resolved instead of reported as ambiguous.

Paths go through the filesystem tools' root checks, and `apply_patch` through their write checks
(protected paths, no symbolic links). This is path validation inside the Hollis process, not a
sandbox (ADR-0016).
"""

import ast
import fnmatch
import os
import re
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, cast

from pydantic import Field, JsonValue

from hollis.runtime.tools import (
    Tool,
    ToolArgumentError,
    ToolContext,
    ToolDescriptor,
    ToolEffect,
    ToolError,
    ToolOutput,
)
from hollis.tools.builtin.filesystem import resolve_for_write, resolve_within
from hollis.tools.function import FunctionTool

SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".hollis", "node_modules", "__pycache__", ".venv", "venv",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", ".next",
        "target", ".idea", ".gradle",
    }
)  # fmt: skip
MAX_FILE_BYTES = 1_000_000
MAX_LINE_CHARS = 1000
SEARCH_SECONDS = 5.0
MAX_FILES = 20_000
MAX_READ_LINES = 400
MAX_PATCH_BYTES = 500_000
# Nested quantifiers such as (a+)+ or (.*)* can take exponential time in Python's re.
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{]")

RelPath = Annotated[
    str, Field(min_length=1, max_length=1024, description="Path relative to the root")
]


def _text(path: Path) -> str | None:
    """The file's text, or None if it is too large or looks binary."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def _files(root: Path, start: Path, glob: str | None) -> Iterator[Path]:
    for directory, dirs, names in os.walk(start):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith(".hollis"))
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_symlink():
                continue
            relative = path.relative_to(root).as_posix()
            if glob is None or fnmatch.fnmatch(relative, glob) or fnmatch.fnmatch(name, glob):
                yield path


def code_tools(
    root: str | os.PathLike[str],
    *,
    writable: bool = False,
    prefix: str = "code",
    protected: Sequence[str] = (),
) -> list[Tool]:
    base = Path(root)
    real_root = base.resolve(strict=True)
    if not real_root.is_dir():
        raise ValueError(f"{root} is not a directory")

    def search(
        query: Annotated[str, Field(min_length=1, max_length=200, description="Text to find")],
        path: RelPath = ".",
        regex: Annotated[
            bool, Field(description="Treat the query as a regular expression")
        ] = False,
        glob: Annotated[
            str | None, Field(max_length=100, description="Only files matching, e.g. *.py")
        ] = None,
        case_sensitive: bool = False,
        max_results: Annotated[int, Field(ge=1, le=200)] = 50,
    ) -> str:
        """Search text files under the root. Answers `path:line: text` for each match."""
        if regex and _NESTED_QUANTIFIER.search(query):
            raise ToolError("nested quantifiers like (a+)+ are not allowed; simplify the pattern")
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query if regex else re.escape(query), flags)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from None
        start = resolve_within(base, path)
        candidates = [start] if start.is_file() else _files(real_root, start, glob)
        hits: list[str] = []
        more = scanned = 0
        deadline = time.monotonic() + SEARCH_SECONDS
        stopped = ""
        for file in candidates:
            if scanned >= MAX_FILES or time.monotonic() > deadline:
                stopped = f"; stopped after {scanned} files (limit), results may be incomplete"
                break
            scanned += 1
            text = _text(file)
            if text is None:
                continue
            relative = file.relative_to(real_root).as_posix()
            for number, line in enumerate(text.splitlines(), start=1):
                if pattern.search(line[:MAX_LINE_CHARS]):
                    if len(hits) < max_results:
                        hits.append(f"{relative}:{number}: {line.strip()[:200]}")
                    else:
                        more += 1
        summary = f"{len(hits) + more} matches in {scanned} files scanned{stopped}"
        if more:
            summary += f" ({more} not shown; narrow the search)"
        return (
            "\n".join([*hits, summary])
            if hits
            else f"no matches ({scanned} files scanned{stopped})"
        )

    def outline(path: RelPath) -> str:
        """The structure of a source or Markdown file, with line numbers."""
        target = resolve_within(base, path)
        text = _text(target) if target.is_file() else None
        if text is None:
            raise ToolError(f"{path} is not a readable text file")
        entries = (
            _python_outline(text)
            if target.suffix == ".py"
            else _pattern_outline(text, target.suffix)
        )
        lines = text.count("\n") + (0 if text.endswith("\n") else 1)
        header = f"{path}: {lines} lines"
        return "\n".join([header, *entries]) if entries else f"{header}; no structure found"

    def read(
        path: RelPath,
        start: Annotated[int, Field(ge=1, description="First line (1-based)")] = 1,
        end: Annotated[int | None, Field(ge=1, description="Last line (inclusive)")] = None,
    ) -> str:
        """Read a file's lines with line numbers (at most 400 per call)."""
        target = resolve_within(base, path)
        text = _text(target) if target.is_file() else None
        if text is None:
            raise ToolError(f"{path} is not a readable text file")
        lines = text.splitlines()
        last = min(end or start + MAX_READ_LINES - 1, start + MAX_READ_LINES - 1, len(lines))
        if start > max(len(lines), 1):
            raise ToolError(f"{path} has {len(lines)} lines")
        width = len(str(last))
        body = [f"{n:>{width}} | {lines[n - 1][:MAX_LINE_CHARS]}" for n in range(start, last + 1)]
        footer = f"[{path}: lines {start}-{last} of {len(lines)}]"
        return "\n".join([*body, footer])

    tools: list[Tool] = [
        FunctionTool(search, name=f"{prefix}_search", effect=ToolEffect.READ_ONLY, timeout_s=15),
        FunctionTool(outline, name=f"{prefix}_outline", effect=ToolEffect.READ_ONLY),
        FunctionTool(read, name=f"{prefix}_read", effect=ToolEffect.READ_ONLY),
    ]
    if writable:
        tools.append(ApplyPatchTool(base, protected))
    return tools


# --- outlines ----------------------------------------------------------------------------------


def _summary(node: ast.AST) -> str:
    doc = (
        ast.get_docstring(node)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Module)
        else None
    )
    return f"  # {doc.strip().splitlines()[0][:100]}" if doc else ""


def _python_outline(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [f"(not valid Python: {exc.msg} at line {exc.lineno})"]
    entries: list[str] = []
    if summary := _summary(tree):
        entries.append(f"1: module{summary}")

    def visit(nodes: list[ast.stmt], indent: str) -> None:
        for node in nodes:
            if isinstance(node, ast.ClassDef):
                bases = ", ".join(ast.unparse(b) for b in node.bases)
                entries.append(f"{node.lineno}: {indent}class {node.name}({bases}){_summary(node)}")
                visit(node.body, indent + "  ")
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
                signature = f"{node.name}({ast.unparse(node.args)}){returns}"
                entries.append(f"{node.lineno}: {indent}{prefix} {signature}{_summary(node)}")
            elif not indent and isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [t.id for t in targets if isinstance(t, ast.Name) and t.id.isupper()]
                entries.extend(f"{node.lineno}: {name} =" for name in names)

    visit(tree.body, "")
    return entries


_JS = (
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)\s*\(",
    r"^\s*(?:export\s+)?class\s+(\w+)",
    r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\(",
)
_JAVA = (
    r"^\s*(?:public|protected|private|static|final|abstract|\s)*"
    r"(?:class|interface|enum|record)\s+(\w+)",
    r"^\s*(?:public|protected|private)\s+[\w<>\[\], ]+\s+(\w+)\s*\(",
)
_MARKDOWN = (r"^(#{1,6})\s+(.+)$",)
_PATTERNS: Mapping[str, tuple[str, ...]] = {
    ".md": _MARKDOWN,
    ".markdown": _MARKDOWN,
    ".js": _JS,
    ".jsx": _JS,
    ".ts": _JS,
    ".tsx": _JS,
    ".go": (r"^func\s+(?:\([^)]*\)\s*)?(\w+)\s*\(", r"^type\s+(\w+)\s+(?:struct|interface)"),
    ".rs": (
        r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)",
        r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+(\w+)",
        r"^\s*impl\b(.*)\{",
    ),
    ".java": _JAVA,
    ".kt": _JAVA,
    ".cs": _JAVA,
    ".rb": (r"^\s*(?:def|class|module)\s+([\w:.?!]+)",),
}


def _pattern_outline(text: str, suffix: str) -> list[str]:
    patterns = [re.compile(p) for p in _PATTERNS.get(suffix.lower(), ())]
    entries: list[str] = []
    fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if suffix.lower() in (".md", ".markdown") and line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if fence:
            continue
        for pattern in patterns:
            if pattern.search(line[:MAX_LINE_CHARS]):
                entries.append(f"{number}: {line.strip()[:160]}")
                break
    return entries[:500]


# --- patches -----------------------------------------------------------------------------------


@dataclass
class _Hunk:
    old_start: int
    lines: list[str] = field(default_factory=list[str])
    """Each line keeps its marker: ' ' context, '-' removed, '+' added."""

    def old(self) -> list[str]:
        return [line[1:] for line in self.lines if line[0] in " -"]

    def new(self) -> list[str]:
        return [line[1:] for line in self.lines if line[0] in " +"]


@dataclass
class _FilePatch:
    old_path: str | None
    new_path: str | None
    hunks: list[_Hunk] = field(default_factory=list[_Hunk])
    no_newline_old: bool = False
    no_newline_new: bool = False

    @property
    def target(self) -> str:
        path = self.new_path or self.old_path
        assert path is not None  # noqa: S101  # the parser never produces both None
        return path


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _patch_path(raw: str) -> str | None:
    path = raw.split("\t", 1)[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def parse_patch(text: str) -> list[_FilePatch]:
    """Parse a unified diff (one or more files). Raises ToolArgumentError when malformed."""
    files: list[_FilePatch] = []
    current: _FilePatch | None = None
    hunk: _Hunk | None = None
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if (
            line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            current = _FilePatch(_patch_path(line[4:]), _patch_path(lines[index + 1][4:]))
            if current.old_path is None and current.new_path is None:
                raise ToolArgumentError("a file patch cannot have /dev/null on both sides")
            files.append(current)
            hunk = None
            index += 2
            continue
        match = _HUNK.match(line)
        if match:
            if current is None:
                raise ToolArgumentError(f"hunk before any file header (line {index + 1})")
            hunk = _Hunk(old_start=int(match.group(1)))
            current.hunks.append(hunk)
        elif line.startswith("\\"):
            if hunk is not None and hunk.lines and current is not None:
                if hunk.lines[-1][0] in " -":
                    current.no_newline_old = True
                if hunk.lines[-1][0] in " +":
                    current.no_newline_new = True
        elif hunk is not None and line[:1] in (" ", "-", "+"):
            hunk.lines.append(line)
        elif hunk is not None and line == "":
            hunk.lines.append(" ")  # an empty context line whose leading space was stripped
        index += 1
    if not files:
        raise ToolArgumentError("no file headers (--- a/path / +++ b/path) found in the patch")
    for patch in files:
        if not patch.hunks and patch.new_path is not None and patch.old_path is not None:
            raise ToolArgumentError(f"{patch.target}: no hunks")
    return files


def _locate(content: list[str], old: list[str], expected: int, after: int) -> int:
    """Where `old` occurs in `content` at or after `after`: the expected line if it matches
    there, else its only occurrence. Raises ToolError when it is missing or ambiguous."""
    if not old:
        return min(max(expected, after), len(content))
    if content[expected : expected + len(old)] == old and expected >= after:
        return expected
    found = [
        i for i in range(after, len(content) - len(old) + 1) if content[i : i + len(old)] == old
    ]
    if len(found) == 1:
        return found[0]
    first = old[0].strip()[:80]
    if not found:
        raise ToolError(f"context not found (expected near line {expected + 1}: {first!r})")
    lines = ", ".join(str(i + 1) for i in found[:5])
    raise ToolError(f"context is ambiguous (matches at lines {lines}); include more context")


def _apply(content: str | None, patch: _FilePatch) -> str | None:
    """New content of one file, or None when the patch deletes it."""
    if patch.new_path is None:
        if content is None:
            raise ToolError(f"{patch.target}: cannot delete a file that does not exist")
        expected = "\n".join(line for h in patch.hunks for line in h.old())
        if content.rstrip("\n") != expected.rstrip("\n"):
            raise ToolError(f"{patch.target}: the file differs from the patch; not deleted")
        return None
    if patch.old_path is None:
        if content is not None:
            raise ToolError(f"{patch.target}: already exists; not created")
        body = [line for h in patch.hunks for line in h.new()]
        return "\n".join(body) + ("" if patch.no_newline_new or not body else "\n")
    if content is None:
        raise ToolError(f"{patch.target}: does not exist")
    lines = content.splitlines()
    trailing = content.endswith("\n")
    offset = 0
    cursor = 0
    for number, hunk in enumerate(patch.hunks, start=1):
        try:
            at = _locate(lines, hunk.old(), max(hunk.old_start - 1 + offset, 0), cursor)
        except ToolError as exc:
            raise ToolError(f"{patch.target}, hunk {number}: {exc}") from None
        old, new = hunk.old(), hunk.new()
        lines[at : at + len(old)] = new
        offset += len(new) - len(old)
        cursor = at + len(new)
    if patch.no_newline_new:
        trailing = False
    elif patch.no_newline_old:
        trailing = True
    return "\n".join(lines) + ("\n" if trailing and lines else "")


def _reverse(patch: _FilePatch) -> _FilePatch:
    flip = {" ": " ", "-": "+", "+": "-"}
    return _FilePatch(
        old_path=patch.new_path,
        new_path=patch.old_path,
        hunks=[
            _Hunk(old_start=h.old_start, lines=[flip[line[0]] + line[1:] for line in h.lines])
            for h in patch.hunks
        ],
        no_newline_old=patch.no_newline_new,
        no_newline_new=patch.no_newline_old,
    )


PATCH_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "patch": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_PATCH_BYTES,
            "description": "A unified diff: '--- a/path' and '+++ b/path' headers, then "
            "'@@' hunks with a few lines of unchanged context. Use /dev/null to create or delete "
            "a file. Several files may be patched at once; all apply or none do.",
        }
    },
    "required": ["patch"],
    "additionalProperties": False,
}


class ApplyPatchTool:
    """`apply_patch`: change files with a unified diff, all or nothing."""

    def __init__(self, root: Path, protected: Sequence[str] = ()) -> None:
        self._root = root
        self._protected = tuple(protected)
        self._descriptor = ToolDescriptor(
            name="apply_patch",
            version="1",
            description=(
                "Change files under the root with a unified diff. Hunks are placed by their "
                "context, so include 2-3 unchanged lines around each change; a hunk whose context "
                "is missing or matches several places refuses the whole patch, and nothing is "
                "changed. Answers which files changed."
            ),
            input_schema=PATCH_SCHEMA,
            effect=ToolEffect.MUTATING,
            idempotent=False,  # applying the same patch twice fails (or would double-apply)
            reconcilable=True,  # whether it was already applied can be checked
            timeout_s=30,
        )

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._descriptor

    def validate(self, arguments: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        patch = arguments.get("patch")
        extra = set(arguments) - {"patch", "paths"}
        if extra:
            raise ToolArgumentError(f"unknown arguments: {sorted(extra)}")
        if not isinstance(patch, str) or not patch.strip():
            raise ToolArgumentError("patch: a non-empty unified diff is required")
        if len(patch.encode()) > MAX_PATCH_BYTES:
            raise ToolArgumentError(f"patch: larger than {MAX_PATCH_BYTES} bytes")
        files = parse_patch(patch)
        paths: list[str] = []
        for file in files:
            for path in {file.old_path, file.new_path} - {None}:
                try:
                    resolve_for_write(self._root, cast(str, path), self._protected)
                except ToolError as exc:
                    raise ToolArgumentError(f"{path}: {exc}") from None
            paths.append(file.target)
        # `paths` is derived (shown to approvers, matched by policy overlays), never trusted input.
        return {"patch": patch, "paths": cast(list[JsonValue], sorted(set(paths)))}

    def _plan(self, files: list[_FilePatch]) -> dict[Path, str | None]:
        planned: dict[Path, str | None] = {}
        for file in files:
            target = resolve_for_write(self._root, file.target, self._protected)
            if target.exists() and not target.is_file():
                raise ToolError(f"{file.target}: not a regular file")
            current = planned[target] if target in planned else _read_existing(target)
            planned[target] = _apply(current, file)
        return planned

    async def invoke(self, arguments: dict[str, JsonValue], context: ToolContext) -> ToolOutput:
        files = parse_patch(str(arguments["patch"]))
        planned = self._plan(files)  # everything is checked before anything is written
        _write_all(planned)
        added = sum(1 for f in files for h in f.hunks for line in h.lines if line[0] == "+")
        removed = sum(1 for f in files for h in f.hunks for line in h.lines if line[0] == "-")
        changed = ", ".join(sorted(f.target for f in files))
        return ToolOutput.text(f"patched {len(planned)} file(s): {changed} (+{added} -{removed})")

    async def reconcile(self, context: ToolContext) -> ToolOutput | None:
        """After an interrupted attempt: applied if the reverse patch applies cleanly (its
        changes are present); not applied if the patch itself still applies (nothing was
        written); otherwise unknown (None: the runner records the outcome as ambiguous)."""
        patch = (context.arguments or {}).get("patch")
        if not isinstance(patch, str):
            return None
        files = parse_patch(patch)
        if self._applies([_reverse(f) for f in files]):
            return ToolOutput.text("the patch had already been applied before the interruption")
        if self._applies(files):
            return ToolOutput.text(
                "the patch was not applied before the interruption; nothing changed",
                is_error=True,
            )
        return None

    def _applies(self, files: list[_FilePatch]) -> bool:
        try:
            self._plan(files)
        except (ToolError, ToolArgumentError):
            return False
        return True


def _read_existing(path: Path) -> str | None:
    if not path.exists():
        return None
    text = _text(path)
    if text is None:
        raise ToolError(f"{path.name} is binary or too large to patch")
    return text


def _write_all(planned: Mapping[Path, str | None]) -> None:
    """Write every file through a temporary file, then move them all into place. If a move
    fails, files already moved are restored, so a patch never half-applies."""
    staged: list[tuple[Path, str | None]] = []
    backups: dict[Path, bytes | None] = {}
    try:
        for target, content in planned.items():
            backups[target] = target.read_bytes() if target.exists() else None
            if content is None:
                staged.append((target, None))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".hollis-patch-")
            with os.fdopen(fd, "wb") as handle:
                handle.write(content.encode("utf-8"))
            staged.append((target, temp))
        done: list[Path] = []
        try:
            for target, temp in staged:
                if temp is None:
                    target.unlink()
                else:
                    os.replace(temp, target)
                done.append(target)
        except OSError:
            for target in done:
                original = backups[target]
                if original is None:
                    target.unlink(missing_ok=True)
                else:
                    target.write_bytes(original)
            raise
    finally:
        for _, temp in staged:
            if temp is not None:
                Path(temp).unlink(missing_ok=True)
