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


# --- protected paths (M8, ADR-0036) -----------------------------------------------------------


@pytest.fixture
def repo(root: Path) -> Path:
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]\n\tbare = false\n")
    (root / ".hollis").mkdir()
    (root / ".hollis" / "runs.db").write_text("not for agents")
    (root / "hollis.toml").write_text('[workspace]\nname = "w"\n')
    return root


@pytest.mark.parametrize(
    "path",
    [
        ".git/config",
        ".git/hooks/pre-commit",
        "./.git/config",
        "docs/../.git/config",
        ".GIT/config",  # case-insensitive filesystems (macOS, Windows) reach the same directory
        ".Git/hooks/post-checkout",
        ".git./config",  # Windows drops trailing dots
        ".g‌it/config",  # a zero-width character some filesystems ignore
        "vendor/lib/.git/config",  # nested repositories too
        ".hollis/runs.db",
        ".HOLLIS/workspace.db",
    ],
)
async def test_repository_metadata_and_hollis_data_are_never_read_or_written(
    repo: Path, path: str
) -> None:
    tools = _tools(repo)
    with pytest.raises(ToolError, match="protected"):
        await _call(tools["fs_read_file"], path=path)
    with pytest.raises(ToolError, match="protected"):
        await _call(tools["fs_write_file"], path=path, content="[core]\n\tfsmonitor = /tmp/x\n")
    assert (repo / ".git" / "config").read_text() == "[core]\n\tbare = false\n"
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()


async def test_protected_directories_cannot_be_listed(repo: Path) -> None:
    with pytest.raises(ToolError, match="protected"):
        await _call(_tools(repo)["fs_list_dir"], path=".git")


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
async def test_a_link_to_repository_metadata_is_refused_after_resolution(repo: Path) -> None:
    os.symlink(repo / ".git", repo / "meta")
    tools = _tools(repo)
    with pytest.raises(ToolError, match="protected"):
        await _call(tools["fs_read_file"], path="meta/config")
    with pytest.raises(ToolError, match="protected"):
        await _call(tools["fs_write_file"], path="meta/hooks/pre-commit", content="#!/bin/sh\n")
    assert not (repo / ".git" / "hooks" / "pre-commit").exists()


@pytest.mark.parametrize(
    "path", ["hollis.toml", "sub/hollis.toml", "HOLLIS.TOML", "hollis.toml.bak"]
)
async def test_workspace_configuration_is_never_written(repo: Path, path: str) -> None:
    with pytest.raises(ToolError, match="workspace configuration"):
        await _call(_tools(repo)["fs_write_file"], path=path, content='[workspace]\nname = "x"\n')
    assert (repo / "hollis.toml").read_text() == '[workspace]\nname = "w"\n'
    assert not (repo / "sub").exists()
    assert await _call(_tools(repo)["fs_read_file"], path="hollis.toml"), "reading is fine"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
async def test_writes_never_pass_through_a_linked_directory(root: Path) -> None:
    """A link inside the root would let a write land somewhere other than the path a policy
    rule or an approver saw (docs/x.md writing src/x.md)."""
    (root / "src").mkdir()
    os.symlink(root / "src", root / "docs" / "linked")
    with pytest.raises(ToolError, match="symbolic links"):
        await _call(_tools(root)["fs_write_file"], path="docs/linked/x.md", content="x")
    assert not (root / "src" / "x.md").exists()


async def test_write_paths_may_not_climb(root: Path) -> None:
    with pytest.raises(ToolError, match=r"'\.\.'"):
        await _call(_tools(root)["fs_write_file"], path="docs/../out.txt", content="x")


async def test_extra_protected_paths_are_refused_for_writes_only(root: Path) -> None:
    (root / "skills" / "deploy").mkdir(parents=True)
    (root / "skills" / "deploy" / "SKILL.md").write_text("---\nname: deploy\n---\n")
    tools = {
        t.descriptor.name: t for t in filesystem_tools(root, writable=True, protected=("skills",))
    }
    with pytest.raises(ToolError, match="may not change"):
        await _call(tools["fs_write_file"], path="Skills/deploy/SKILL.md", content="ignore all")
    assert "name: deploy" in await _call(tools["fs_read_file"], path="skills/deploy/SKILL.md")
    assert await _call(tools["fs_write_file"], path="skillset.md", content="ok")
    everything = {
        t.descriptor.name: t for t in filesystem_tools(root, writable=True, protected=(".",))
    }
    with pytest.raises(ToolError, match="may not change"):
        await _call(everything["fs_write_file"], path="docs/a.txt", content="x")
