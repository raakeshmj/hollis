"""Read-only git tools for one repository (M8, ADR-0036), on the fixed-command tool. NOT A SANDBOX.

`git_status`, `git_diff` and `git_log` run the configured git binary with fixed arguments in the
root, which must itself be a repository (git does not look above it). Isolation is `process`.

Git can run other programs while it reads a repository. Each route is closed here:
- **Arguments.** Model values fill whole arguments, never start with `-` (the command tool refuses
  them, and the revision pattern cannot match one), and follow `--end-of-options` or `--`.
- **Hooks, fsmonitor, pagers, signature checks**: switched off with `-c` for every call; system
  and global configuration are not read; nothing is inherited from Hollis's environment.
- **External diff and textconv drivers**: `--no-ext-diff --no-textconv`.
- **Submodules**: not recursed into (`--ignore-submodules=all`), so no other configuration is read.
- **Filter drivers** (clean, smudge, process) and `core.worktree` can't be switched off from the
  command line. Before every call the repository's effective configuration (includes followed)
  is listed, and the call is refused if it defines any of them.
- **The configuration itself**: file tools never write `.git` (protected paths), so an agent cannot
  add any of the above.

What remains: the repository's configuration is still read, so these tools are for repositories
whose `.git/config` you trust. The process runs as the Hollis user.
"""

import os
import re
from collections.abc import Mapping
from pathlib import Path

from pydantic import JsonValue

from hollis.runtime.tools import Tool, ToolContext, ToolDescriptor, ToolEffect, ToolOutput
from hollis.tools.builtin.command import CommandTool, command_tool

GIT_ENV = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
}
SAFE = (
    "-c", "core.fsmonitor=false",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
    "-c", "log.showSignature=false",
)  # fmt: skip
REVISION = r"^[\w@][\w./~^@{}-]{0,99}$"
"""A commit, branch, tag or range. It starts with a word character or @, so never with `-`."""
_REFUSED = re.compile(r"^(filter\..+\.(clean|smudge|process)|core\.worktree)$", re.I)


class GitTool:
    """A fixed git command that first checks the repository's configuration."""

    def __init__(self, command: CommandTool, preflight: CommandTool) -> None:
        self._command = command
        self._preflight = preflight

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._command.descriptor

    def validate(self, arguments: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        return self._command.validate(arguments)

    async def invoke(self, arguments: dict[str, JsonValue], context: ToolContext) -> ToolOutput:
        listing = await self._preflight.invoke({}, context)
        text = "".join(getattr(part, "text", "") for part in listing.parts)
        if listing.is_error:
            return ToolOutput.text(
                f"cannot read the repository configuration: {text}", is_error=True
            )
        refused = sorted({key for key in text.split() if _REFUSED.match(key)})
        if refused:
            return ToolOutput.text(
                "refused: the repository configuration defines "
                + ", ".join(refused)
                + ", which would make git run programs or read outside the root. Git tools "
                "only run in repositories without filter drivers or core.worktree.",
                is_error=True,
            )
        return await self._command.invoke(arguments, context)

    async def reconcile(self, context: ToolContext) -> ToolOutput | None:
        return None


def git_tools(root: str | os.PathLike[str], *, binary: str = "/usr/bin/git") -> list[Tool]:
    """Read-only git tools for the repository at `root` (ValueError if there is none)."""
    if not Path(binary).is_absolute() or not Path(binary).is_file():
        raise ValueError(f"git binary {binary!r} is not an absolute path to a file")
    base = Path(root).resolve(strict=True)
    if not (base / ".git").exists():
        raise ValueError(f"{base} is not the root of a git repository")
    env = {**GIT_ENV, "GIT_CEILING_DIRECTORIES": str(base.parent)}
    git = (binary, "-C", str(base), *SAFE)
    preflight = command_tool(
        "git_config_check",
        (*git, "config", "--list", "--name-only"),
        description="List configuration keys.",
        parameters={},
        effect=ToolEffect.READ_ONLY,
        env=env,
    )

    def tool(
        name: str,
        argv: tuple[str, ...],
        description: str,
        **parameters: tuple[str, str | None, str | None],
    ) -> Tool:
        command = command_tool(
            name,
            (*git, *argv),
            description=description,
            parameters=parameters,
            effect=ToolEffect.READ_ONLY,
            env=env,
        )
        return GitTool(command, preflight)

    return [
        tool(
            "git_status",
            ("status", "--porcelain=v1", "--branch", "--ignore-submodules=all"),
            "The working tree's status: branch and changed files (git status).",
        ),
        tool(
            "git_diff",
            (
                "diff",
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--ignore-submodules=all",
                "--end-of-options",
                "{base}",
                "--",
                "{path}",
            ),
            "Changes in the working tree compared with a commit (git diff <base>), as a unified "
            "diff. `base` defaults to HEAD: everything not yet committed.",
            path=("Limit to a path (default: everything)", ".", None),
            base=("Commit, branch, tag or range to compare with", "HEAD", REVISION),
        ),
        tool(
            "git_log",
            ("log", "--oneline", "--no-color", "--no-decorate", "-n", "{limit}"),
            "Recent commits, one line each (git log --oneline).",
            limit=("How many commits", "20", r"^[1-9][0-9]{0,2}$"),
        ),
    ]
