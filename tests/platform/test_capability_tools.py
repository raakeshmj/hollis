"""M8 capability tools in a workspace: which tools a configuration yields, configured commands
(patterns, options, environment, names), and the paths file tools protect."""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from hollis.platform.config import initialize
from hollis.platform.services import Platform
from hollis.runtime.tools import Tool, ToolArgumentError, ToolContext, ToolError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX commands")
PY = sys.executable
GIT = next((p for p in ("/usr/bin/git", shutil.which("git")) if p and Path(p).is_file()), None)

COMMANDS = f"""
[[tools.commands]]
name = "echo_ticket"
description = "Echo a ticket id."
argv = ["{PY}", "-c", "import sys; print(sys.argv[1])", "{{ticket}}"]
effect = "read_only"
parameters = {{ ticket = {{ description = "Ticket id", pattern = "[A-Z]+-[0-9]+" }} }}

[[tools.commands]]
name = "show_env"
description = "Print the environment."
argv = ["{PY}", "-c", "import os; print(sorted(os.environ.items()))"]
effect = "read_only"
env = {{ MODE = "check" }}
inherit_env = ["HOLLIS_TEST_ALLOWED"]

[[tools.commands]]
name = "deploy"
description = "Deploy (changes things)."
argv = ["{PY}", "-c", "print('deployed')"]

[[tools.commands]]
name = "fs_read_file"
description = "Would shadow a built-in tool."
argv = ["{PY}", "-c", "print('shadow')"]
effect = "read_only"

[[tools.commands]]
name = "delegate_everyone"
description = "Would look like a delegation."
argv = ["{PY}", "-c", "print('x')"]

[[tools.commands]]
name = "relative"
description = "Relative program."
argv = ["python3", "-c", "print('x')"]
"""


async def _open(tmp_path: Path, extra: str = "", *, git: bool = False) -> Platform:
    initialize(tmp_path, "caps")
    config = tmp_path / "hollis.toml"
    config.write_text(config.read_text() + extra)
    if git:
        assert GIT is not None
        env = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/dev/null"}
        subprocess.run([GIT, "init", "-q", str(tmp_path)], check=True, env=env)  # noqa: S603  # fixed git binary
        config.write_text(config.read_text() + f'\n[tools.git]\nbinary = "{GIT}"\n')
    return await Platform.open(tmp_path)


def _tool(platform: Platform, name: str) -> Tool:
    tool = platform.ws.registry.get(name)
    assert tool is not None, name
    return tool


async def _invoke(platform: Platform, name: str, **arguments: Any) -> tuple[str, bool]:
    tool = _tool(platform, name)
    output = await tool.invoke(
        tool.validate(arguments), ToolContext(run_id=uuid4(), invocation_id="i", attempt=1)
    )
    return "".join(getattr(p, "text", "") for p in output.parts), output.is_error


async def test_a_filesystem_root_yields_code_data_and_docs_tools(tmp_path: Path) -> None:
    platform = await _open(tmp_path)
    try:
        admin = await platform.local_admin()
        views = {t.name: t for t in await platform.tools(admin)}
        for name, source in [
            ("code_search", "builtin:code"),
            ("apply_patch", "builtin:code"),
            ("data_query", "builtin:data"),
            ("docs_check", "builtin:docs"),
        ]:
            assert views[name].source == source
        assert views["code_search"].policy == "allow", "trusted read-only tools run"
        assert views["apply_patch"].policy == "require_approval", "patches need approval"
        assert not any(n.startswith("git_") for n in views), "git tools are opt-in"
    finally:
        await platform.close()


@pytest.mark.skipif(GIT is None, reason="git is not installed")
async def test_git_tools_when_configured_for_a_repository(tmp_path: Path) -> None:
    platform = await _open(tmp_path, git=True)
    try:
        admin = await platform.local_admin()
        views = {t.name: t for t in await platform.tools(admin)}
        assert {"git_status", "git_diff", "git_log"} <= set(views)
        assert views["git_diff"].isolation == "process"
        text, error = await _invoke(platform, "git_status")
        assert not error
        assert "hollis.toml" in text
    finally:
        await platform.close()


async def test_configured_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOLLIS_TEST_ALLOWED", "visible")
    monkeypatch.setenv("HOLLIS_TEST_SECRET", "sk-should-not-leak")
    platform = await _open(tmp_path, COMMANDS)
    try:
        admin = await platform.local_admin()
        views = {t.name: t for t in await platform.tools(admin)}
        assert views["echo_ticket"].source == "builtin:commands"
        assert views["echo_ticket"].isolation == "process"
        assert views["deploy"].effect == "mutating", "commands are assumed to change things"
        assert views["deploy"].policy == "require_approval"
        assert views["fs_read_file"].source == "builtin:filesystem", "no shadowing built-ins"
        assert "delegate_everyone" not in views, "reserved prefix"
        assert "relative" not in views, "programs need an absolute path"

        assert (await _invoke(platform, "echo_ticket", ticket="OPS-42"))[0].strip() == "OPS-42"
        for value in ("OPS-42; rm -rf /", "x OPS-42", "OPS-42\n", "-OPS-42", "--help"):
            with pytest.raises(ToolArgumentError):
                _tool(platform, "echo_ticket").validate({"ticket": value})

        env, _ = await _invoke(platform, "show_env")
        assert "('MODE', 'check')" in env
        assert "('HOLLIS_TEST_ALLOWED', 'visible')" in env
        assert "sk-should-not-leak" not in env
        assert "HOLLIS_TEST_SECRET" not in env
        assert "('PATH', '/usr/bin:/bin')" in env
    finally:
        await platform.close()


async def test_workspace_files_are_protected_from_file_tools(tmp_path: Path) -> None:
    """`hollis init` makes the workspace root writable by agents (with approval): its
    configuration and data still are not."""
    platform = await _open(tmp_path)
    try:
        for path in ("hollis.toml", ".hollis/workspace.db", ".hollis/.gitignore"):
            with pytest.raises(ToolError, match=r"protected|workspace configuration"):
                await _invoke(platform, "fs_write_file", path=path, content="x")
        before = (tmp_path / "hollis.toml").read_text()
        patch = "--- a/hollis.toml\n+++ b/hollis.toml\n@@ -1 +1 @@\n-#\n+[policy]\n"
        with pytest.raises(ToolArgumentError, match="workspace configuration"):
            _tool(platform, "apply_patch").validate({"patch": patch})
        assert (tmp_path / "hollis.toml").read_text() == before
    finally:
        await platform.close()
