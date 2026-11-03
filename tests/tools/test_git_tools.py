"""Read-only git tools: what they answer, and the ways git could run a program that stay closed.

Every adversarial test plants a script that leaves a marker file when executed, configures the
repository to run it, then calls the tools. The marker must never appear.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from hollis.runtime.tools import Tool, ToolArgumentError, ToolContext, ToolEffect
from hollis.tools.builtin import git_tools

GIT = next((p for p in ("/usr/bin/git", shutil.which("git")) if p and Path(p).is_file()), None)
pytestmark = [
    pytest.mark.skipif(GIT is None, reason="git is not installed"),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX scripts"),
]
CLEAN_ENV = {
    "PATH": "/usr/bin:/bin",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(repo: Path, *args: str) -> None:
    assert GIT is not None
    subprocess.run(  # noqa: S603  # fixed git binary, test-controlled arguments
        [GIT, "-C", str(repo), *args], check=True, env=CLEAN_ENV, capture_output=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "app.py").write_text("print('one')\n")
    _git(root, "add", "app.py")
    _git(root, "commit", "-q", "-m", "first commit")
    (root / "app.py").write_text("print('two')\n")
    return root


@pytest.fixture
def evil(tmp_path: Path) -> tuple[Path, Path]:
    """A program that proves it ran by creating a marker file."""
    marker = tmp_path / "PWNED"
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\ncat\n")
    script.chmod(0o755)
    return script, marker


def _tools(repo: Path) -> dict[str, Tool]:
    assert GIT is not None
    return {t.descriptor.name: t for t in git_tools(repo, binary=GIT)}


async def _call(tool: Tool, **arguments: Any) -> tuple[str, bool]:
    context = ToolContext(run_id=uuid4(), invocation_id="i", attempt=1)
    output = await tool.invoke(tool.validate(arguments), context)
    return "".join(getattr(p, "text", "") for p in output.parts), output.is_error


async def test_status_diff_and_log_answer(repo: Path) -> None:
    tools = _tools(repo)
    status, error = await _call(tools["git_status"])
    assert not error
    assert "## main" in status
    assert " M app.py" in status
    diff, error = await _call(tools["git_diff"])
    assert not error
    assert "-print('one')" in diff
    assert "+print('two')" in diff
    log, error = await _call(tools["git_log"], limit="5")
    assert not error
    assert "first commit" in log
    assert all(t.descriptor.effect is ToolEffect.READ_ONLY for t in tools.values())
    assert all(t.descriptor.isolation.value == "process" for t in tools.values())


@pytest.mark.parametrize(
    "base", ["--ext-diff", "--textconv", "--output=/tmp/x", "-p", "--no-index", "-c"]
)
async def test_revision_values_can_never_be_options(repo: Path, base: str) -> None:
    with pytest.raises(ToolArgumentError):
        _tools(repo)["git_diff"].validate({"base": base})


async def test_an_external_diff_in_the_repository_configuration_never_runs(
    repo: Path, evil: tuple[Path, Path]
) -> None:
    script, marker = evil
    _git(repo, "config", "diff.external", str(script))
    diff, error = await _call(_tools(repo)["git_diff"])
    assert not error
    assert "+print('two')" in diff
    assert not marker.exists()


async def test_textconv_drivers_never_run(repo: Path, evil: tuple[Path, Path]) -> None:
    script, marker = evil
    _git(repo, "config", "diff.evil.textconv", str(script))
    (repo / ".gitattributes").write_text("*.py diff=evil\n")
    _, error = await _call(_tools(repo)["git_diff"])
    assert not error
    assert not marker.exists()


async def test_fsmonitor_and_hooks_are_switched_off(repo: Path, evil: tuple[Path, Path]) -> None:
    script, marker = evil
    _git(repo, "config", "core.fsmonitor", str(script))
    hooks = repo / "hooks"
    hooks.mkdir()
    for hook in ("post-index-change", "reference-transaction"):
        shutil.copy(script, hooks / hook)
    _git(repo, "config", "core.hooksPath", str(hooks))
    tools = _tools(repo)
    for name in ("git_status", "git_diff", "git_log"):
        _, error = await _call(tools[name])
        assert not error, name
    assert not marker.exists()


@pytest.mark.parametrize("kind", ["clean", "smudge", "process"])
async def test_repositories_with_filter_drivers_are_refused(
    repo: Path, evil: tuple[Path, Path], kind: str
) -> None:
    """A clean filter runs whenever git compares the working tree; no flag switches it off."""
    script, marker = evil
    _git(repo, "config", f"filter.evil.{kind}", str(script))
    (repo / ".gitattributes").write_text("*.py filter=evil\n")
    tools = _tools(repo)
    for name in ("git_status", "git_diff", "git_log"):
        text, error = await _call(tools[name])
        assert error, name
        assert f"filter.evil.{kind}" in text, name
    assert not marker.exists()


async def test_filters_from_included_configuration_are_refused(
    repo: Path, evil: tuple[Path, Path]
) -> None:
    """Includes are followed when the configuration is checked: a filter defined in a file the
    repository's configuration includes is seen too."""
    script, marker = evil
    shared = repo / "shared.gitconfig"
    shared.write_text(f'[filter "evil"]\n\tclean = {script}\n')
    _git(repo, "config", "include.path", str(shared))
    (repo / ".gitattributes").write_text("*.py filter=evil\n")
    text, error = await _call(_tools(repo)["git_status"])
    assert error
    assert "filter.evil.clean" in text
    assert not marker.exists()


async def test_a_worktree_elsewhere_is_refused(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.txt").write_text("not under the root\n")
    _git(repo, "config", "core.worktree", str(outside))
    text, error = await _call(_tools(repo)["git_status"])
    assert error
    assert "core.worktree" in text
    assert "private.txt" not in text


def test_the_root_must_be_a_repository_and_git_does_not_look_above_it(repo: Path) -> None:
    inner = repo / "pkg"
    inner.mkdir()
    with pytest.raises(ValueError, match="not the root of a git repository"):
        _tools(inner)
    with pytest.raises(ValueError, match="absolute path"):
        git_tools(repo, binary="git")


async def test_discovery_stops_at_the_root(repo: Path) -> None:
    """Even if the root's own .git disappears, git does not fall back to a parent repository."""
    parent = repo.parent
    _git(parent, "init", "-q")
    tools = _tools(repo)
    shutil.rmtree(repo / ".git")
    text, error = await _call(tools["git_status"])
    assert error
    assert "not a git repository" in text.lower()
    assert os.path.isdir(parent / ".git")
