"""Documentation tools confined to one root directory (M8).

`docs_check` lints Markdown the way a reviewer would before merging documentation: relative
links and images whose target does not exist, `#anchors` that match no heading (GitHub-style
slugs), skipped heading levels, more than one top-level heading, empty links and unclosed code
fences. It answers `path:line: problem` so each problem can be fixed precisely.
"""

import os
import re
import unicodedata
from pathlib import Path
from typing import Annotated, cast
from urllib.parse import unquote, urlsplit

from pydantic import Field

from hollis.runtime.tools import Tool, ToolEffect, ToolError
from hollis.tools.builtin.code import SKIP_DIRS
from hollis.tools.builtin.filesystem import PROTECTED_DIRS, resolve_within
from hollis.tools.function import FunctionTool

MAX_FILES = 500
MAX_FILE_BYTES = 2_000_000
_LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]*)(?:\s+\"[^\"]*\")?\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")

RelPath = Annotated[
    str, Field(min_length=1, max_length=1024, description="Path relative to the root")
]


def _protected(relative: Path) -> bool:
    return any(part.casefold() in PROTECTED_DIRS for part in relative.parts)


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, punctuation removed, spaces to hyphens."""
    text = re.sub(r"`([^`]*)`", r"\1", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = unicodedata.normalize("NFKC", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _headings(text: str) -> list[tuple[int, int, str]]:
    """(line, level, text) of every heading outside code fences."""
    found: list[tuple[int, int, str]] = []
    fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if _FENCE.match(line):
            fence = not fence
            continue
        if not fence and (match := _HEADING.match(line)):
            found.append((number, len(match.group(1)), match.group(2)))
    return found


def _anchors(text: str) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for _, _, heading in _headings(text):
        base = slug(heading)
        count = counts.get(base, 0)
        counts[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def _read(path: Path) -> str:
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ToolError(f"{path.name} is larger than {MAX_FILE_BYTES} bytes")
    return path.read_text(encoding="utf-8", errors="replace")


def check_markdown(path: Path, root: Path) -> list[str]:
    """Problems in one Markdown file, as `relative/path:line: problem`."""
    relative = path.relative_to(root).as_posix()
    text = _read(path)
    problems: list[str] = []
    fence_line: int | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if _FENCE.match(line):
            fence_line = None if fence_line is not None else number
            continue
        if fence_line is not None:
            continue
        links = cast(list[tuple[str, str, str]], _LINK.findall(line))
        for image, label, target in links:
            kind = "image" if image else "link"
            if not target:
                problems.append(f"{relative}:{number}: empty {kind} target")
                continue
            if not label.strip() and not image:
                problems.append(f"{relative}:{number}: link with no text ({target})")
            parts = urlsplit(target)
            if parts.scheme or target.startswith("//") or target.startswith("mailto:"):
                continue  # external links are not checked (no network access)
            file_part, anchor = unquote(parts.path), unquote(parts.fragment)
            destination = path if not file_part else (path.parent / file_part)
            try:
                resolved = destination.resolve()
            except OSError:
                resolved = destination
            if file_part and (not resolved.is_relative_to(root) or not resolved.exists()):
                problems.append(f"{relative}:{number}: broken {kind}: {target} does not exist")
                continue
            if _protected(resolved.relative_to(root)):
                continue  # a link into .git or .hollis: exists, but its content is not read
            markdown = resolved.is_file() and resolved.suffix.lower() in (".md", ".markdown")
            if anchor and markdown and anchor.lower() not in _anchors(_read(resolved)):
                problems.append(f"{relative}:{number}: no heading for anchor #{anchor}")
    if fence_line is not None:
        problems.append(f"{relative}:{fence_line}: code fence is never closed")
    headings = _headings(text)
    top = [n for n, level, _ in headings if level == 1]
    if len(top) > 1:
        problems.append(f"{relative}:{top[1]}: more than one top-level (#) heading")
    previous = 0
    for number, level, heading in headings:
        if previous and level > previous + 1:
            problems.append(
                f"{relative}:{number}: heading level jumps from {previous} to {level} ({heading!r})"
            )
        if not heading.strip():
            problems.append(f"{relative}:{number}: empty heading")
        previous = level
    return problems


def docs_tools(root: str | os.PathLike[str], *, prefix: str = "docs") -> list[Tool]:
    base = Path(root)
    real_root = base.resolve(strict=True)
    if not real_root.is_dir():
        raise ValueError(f"{root} is not a directory")

    def check(path: RelPath = ".") -> str:
        """Check Markdown files (one, or all under a directory): broken links and anchors,
        heading structure, empty links, unclosed code fences."""
        target = resolve_within(base, path)
        if target.is_file():
            files = [target]
        elif target.is_dir():
            files: list[Path] = []
            for directory, dirs, names in os.walk(target):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
                files += [
                    Path(directory) / n
                    for n in sorted(names)
                    if n.lower().endswith((".md", ".markdown"))
                    and not (Path(directory) / n).is_symlink()  # could point outside the root
                ]
        else:
            raise ToolError(f"{path} does not exist")
        if len(files) > MAX_FILES:
            raise ToolError(f"{len(files)} Markdown files; check a smaller directory")
        problems = [p for file in files for p in check_markdown(file, real_root)]
        summary = f"{len(files)} files checked, {len(problems)} problems"
        return "\n".join([*problems, summary]) if problems else f"{summary}: no problems found"

    return [FunctionTool(check, name=f"{prefix}_check", effect=ToolEffect.READ_ONLY)]
