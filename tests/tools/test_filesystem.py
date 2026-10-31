"""Filesystem tools stay inside their root, whatever path the model supplies."""

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from hollis.runtime.tools import Tool, ToolContext, ToolEffect, ToolError
from hollis.tools.builtin import filesystem_tools

CONTEXT = ToolContext(run_id=uuid4(), invocation_id="i", attempt=1)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "a.txt").write_text("hello")
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    return workspace


def _tools(root: Path, writable: bool = True) -> dict[str, Tool]:
    return {
        t.descriptor.name: t for t in filesystem_tools(root, writable=writable, max_read_bytes=100)
    }


async def _call(tool: Tool, **arguments: str) -> str:
    output = await tool.invoke(tool.validate(arguments), CONTEXT)
    return "".join(getattr(p, "text", "") for p in output.parts)


async def test_read_list_and_write_inside_the_root(root: Path) -> None:
    tools = _tools(root)
    assert await _call(tools["fs_read_file"], path="docs/a.txt") == "hello"
    assert "a.txt\tfile\t5" in await _call(tools["fs_list_dir"], path="docs")
    assert (
        await _call(tools["fs_write_file"], path="out/new.txt", content="hi")
        == "wrote 2 bytes to out/new.txt"
    )
    assert (root / "out" / "new.txt").read_text() == "hi"


def test_effects_are_declared_honestly(root: Path) -> None:
    tools = _tools(root)
    assert tools["fs_read_file"].descriptor.effect is ToolEffect.READ_ONLY
    assert tools["fs_write_file"].descriptor.effect is ToolEffect.MUTATING
    assert "fs_write_file" not in _tools(root, writable=False)


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("../secret.txt", "escapes"),
        ("docs/../../secret.txt", "escapes"),
        ("/etc/passwd", "absolute"),
        ("C:\\Windows\\win.ini", "absolute|drive"),
        ("docs/a.txt\x00.png", "NUL"),
    ],
)
async def test_traversal_and_absolute_paths_are_refused(
    root: Path, path: str, message: str
) -> None:
    for name in ("fs_read_file", "fs_write_file"):
        tool = _tools(root)[name]
        arguments = {"path": path, **({"content": "x"} if name == "fs_write_file" else {})}
        with pytest.raises(ToolError, match=message):
            await tool.invoke(tool.validate(arguments), CONTEXT)
    assert (root.parent / "secret.txt").read_text() == "TOP SECRET"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
async def test_symlinks_cannot_escape_or_be_written_through(root: Path) -> None:
    os.symlink(root.parent / "secret.txt", root / "escape.txt")
    os.symlink(root.parent, root / "up")
    os.symlink(root / "docs" / "a.txt", root / "inside-link.txt")
    tools = _tools(root)
    with pytest.raises(ToolError, match="escapes"):
        await _call(tools["fs_read_file"], path="escape.txt")
    with pytest.raises(ToolError, match="escapes"):
        await _call(tools["fs_read_file"], path="up/secret.txt")
    with pytest.raises(ToolError, match="symbolic links"):
        await _call(tools["fs_write_file"], path="inside-link.txt", content="overwrite")
    assert (root / "docs" / "a.txt").read_text() == "hello"


async def test_size_and_binary_limits(root: Path) -> None:
    (root / "big.txt").write_text("x" * 500)
    (root / "blob.bin").write_bytes(b"\x00\x01\x02")
    tools = _tools(root)
    with pytest.raises(ToolError, match="limit is 100"):
        await _call(tools["fs_read_file"], path="big.txt")
    with pytest.raises(ToolError, match="binary"):
        await _call(tools["fs_read_file"], path="blob.bin")


def test_root_must_exist(tmp_path: Path) -> None:
    with pytest.raises((ValueError, FileNotFoundError)):
        filesystem_tools(tmp_path / "missing")
