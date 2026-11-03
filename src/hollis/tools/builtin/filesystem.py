"""Filesystem tools confined to one root directory (ADR-0015).

Every path the model supplies is relative to the root. Absolute paths, NUL bytes, and any path
whose resolved location (after following symlinks) leaves the root are refused. Symlinks are not
followed for the final component of reads and writes. Writes are atomic (temp file + rename).

Protected paths (M8, ADR-0036):
- Repository metadata (`.git`) and Hollis's data directory (`.hollis`) are never read or written,
  by name or through a link, in any letter case: git configuration and hooks can make git run
  programs, remote URLs can hold credentials, and `.hollis` holds every run and memory.
- Workspace configuration (`hollis.toml` and its backup) is never written, at any depth (a nested
  copy would be picked up by the CLI). The workspace adds its own paths (skills, templates).
- Writes never pass through a symbolic link in any component, so the path a policy rule or an
  approver sees is the path that changes.

Limitations, stated plainly: this is path validation inside the Hollis process, not a sandbox.
A concurrent process that can modify the root could race a check (TOCTOU). Use an OS-level
sandbox when the root contains untrusted content or the threat model includes local attackers.
"""

import os
import stat
import tempfile
import unicodedata
from collections.abc import Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated

from pydantic import Field

from hollis.runtime.tools import Tool, ToolEffect, ToolError
from hollis.tools.function import FunctionTool

RelPath = Annotated[
    str, Field(min_length=1, max_length=1024, description="Path relative to the root")
]


PROTECTED_DIRS = frozenset({".git", ".hollis"})
"""Never read or written by file tools."""
PROTECTED_FILES = frozenset({"hollis.toml", "hollis.toml.bak", "hollis.toml.tmp"})
"""Never written by file tools, at any depth."""


def _fold(name: str) -> str:
    """A path component as the filesystem may compare it: case-insensitive, Unicode-normalized,
    without invisible format characters (ignored by some filesystems) or the trailing dots and
    spaces Windows drops."""
    text = unicodedata.normalize("NFC", name)
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")
    return text.rstrip(". ").casefold()


def _components(relative: str) -> list[str]:
    return [part for part in relative.replace("\\", "/").split("/") if part not in ("", ".")]


def _protected_dir(parts: Sequence[str]) -> str | None:
    for part in parts:
        folded = _fold(part)
        if folded in PROTECTED_DIRS or folded == "git~1":  # git~1: a Windows short name
            return part
    return None


def resolve_within(root: Path, relative: str) -> Path:
    """The real path of `relative` under `root`, or ToolError if it would escape or reaches a
    protected directory (by its name, or after following links)."""
    if "\x00" in relative:
        raise ToolError("path contains a NUL byte")
    if PurePosixPath(relative).is_absolute() or PureWindowsPath(relative).is_absolute():
        raise ToolError("absolute paths are not allowed; use a path relative to the root")
    if PureWindowsPath(relative).drive:
        raise ToolError("drive-qualified paths are not allowed")
    if (name := _protected_dir(_components(relative))) is not None:
        raise ToolError(f"{name} is protected: file tools never read or write it")
    real_root = root.resolve(strict=True)
    candidate = (real_root / relative).resolve(strict=False)
    if candidate != real_root and not candidate.is_relative_to(real_root):
        raise ToolError("path escapes the allowed root")
    if (name := _protected_dir(candidate.relative_to(real_root).parts)) is not None:
        raise ToolError(f"{name} is protected: file tools never read or write it")
    return candidate


def resolve_for_write(root: Path, relative: str, protected: Sequence[str] = ()) -> Path:
    """`resolve_within`, and also: no component of the path may be a symbolic link (so the
    path written is the path named), and it may not be workspace configuration or under one of
    the `protected` paths (relative to the root)."""
    target = resolve_within(root, relative)
    parts = _components(relative)
    if ".." in parts:
        raise ToolError("write paths may not contain '..'")
    if parts and _fold(parts[-1]) in PROTECTED_FILES:
        raise ToolError(f"{parts[-1]} is workspace configuration: file tools never write it")
    folded = "/".join(_fold(p) for p in parts)
    for path in protected:
        prefix = "/".join(_fold(p) for p in _components(path))
        if not prefix or folded == prefix or folded.startswith(prefix + "/"):
            raise ToolError(f"{relative} is under {path}, which agents may not change")
    current = root.resolve(strict=True)
    for part in parts:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                raise ToolError(f"{relative}: writes never follow symbolic links")
        except FileNotFoundError:
            break
    if target != root.resolve(strict=True).joinpath(*parts):
        raise ToolError(f"{relative} does not name the file it would change")
    return target


def _no_symlink(path: Path) -> None:
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise ToolError("symbolic links are not followed")
    except FileNotFoundError:
        pass


def filesystem_tools(
    root: str | os.PathLike[str],
    *,
    writable: bool = False,
    prefix: str = "fs",
    max_read_bytes: int = 256_000,
    max_write_bytes: int = 1_000_000,
    max_entries: int = 500,
    protected: Sequence[str] = (),
) -> list[Tool]:
    """Read (and optionally write) tools for files under `root`. `protected`: paths relative to
    the root that writes may not touch (besides the protected names every tool refuses)."""
    base = Path(root)
    if not base.resolve(strict=True).is_dir():
        raise ValueError(f"{root} is not a directory")

    def read_file(path: RelPath) -> str:
        """Read a UTF-8 text file under the workspace root."""
        target = resolve_within(base, path)
        _no_symlink(base.resolve() / path)
        if not target.is_file():
            raise ToolError(f"{path} is not a regular file")
        size = target.stat().st_size
        if size > max_read_bytes:
            raise ToolError(f"{path} is {size} bytes; the limit is {max_read_bytes}")
        data = target.read_bytes()
        if b"\x00" in data:
            raise ToolError(f"{path} looks binary; only text files can be read")
        return data.decode("utf-8", errors="replace")

    def list_dir(path: RelPath = ".") -> str:
        """List a directory under the workspace root (name, type, size)."""
        target = resolve_within(base, path)
        if not target.is_dir():
            raise ToolError(f"{path} is not a directory")
        entries = sorted(target.iterdir(), key=lambda p: p.name)
        lines: list[str] = []
        for entry in entries[:max_entries]:
            info = os.lstat(entry)
            kind = (
                "link"
                if stat.S_ISLNK(info.st_mode)
                else "dir"
                if stat.S_ISDIR(info.st_mode)
                else "file"
            )
            lines.append(f"{entry.name}\t{kind}\t{info.st_size if kind == 'file' else ''}".rstrip())
        if len(entries) > max_entries:
            lines.append(f"[{len(entries) - max_entries} more entries not shown]")
        return "\n".join(lines) or "(empty)"

    def write_file(path: RelPath, content: str) -> str:
        """Create or replace a UTF-8 text file under the workspace root."""
        data = content.encode("utf-8")
        if len(data) > max_write_bytes:
            raise ToolError(f"content is {len(data)} bytes; the limit is {max_write_bytes}")
        target = resolve_for_write(base, path, protected)
        if target == base.resolve():
            raise ToolError("cannot write to the root itself")
        _no_symlink(base.resolve() / path)
        target.parent.mkdir(parents=True, exist_ok=True)
        resolve_within(base, str(target.parent.relative_to(base.resolve())))  # re-check after mkdir
        fd, temp = tempfile.mkstemp(dir=target.parent, prefix=".hollis-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temp, target)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
        return f"wrote {len(data)} bytes to {path}"

    tools: list[Tool] = [
        FunctionTool(read_file, name=f"{prefix}_read_file", effect=ToolEffect.READ_ONLY),
        FunctionTool(list_dir, name=f"{prefix}_list_dir", effect=ToolEffect.READ_ONLY),
    ]
    if writable:
        # Writing the same content to the same path twice has the same effect: idempotent.
        tools.append(
            FunctionTool(
                write_file, name=f"{prefix}_write_file", effect=ToolEffect.MUTATING, idempotent=True
            )
        )
    return tools
