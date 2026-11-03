"""Agent Skills in a workspace (ADR-0033): `skills/<name>/SKILL.md` folders in the open Agent
Skills format (agentskills.io), loaded progressively.

- An agent is *offered* skills by name and description in its instructions (about a hundred
  tokens each), and reads a skill's body, or one of its reference files, on demand with
  `skill_read`.
- Hollis never executes anything in a skill: there is no verified sandbox (ADR-0016). Files
  under `scripts/` are shown as text with that said.
- `allowed-tools` grants nothing. Permissions never come from content; it is shown as
  information only.
- Which skills an agent is offered is guidance, not access control: `skill_read` reads any
  skill of the workspace, like a file tool reads any file under its root.

Frontmatter is the YAML subset the specification uses: `key: value` lines (optionally quoted),
`>` and `|` block scalars, and a one-level `metadata:` mapping. Anything else is reported as a
problem and the skill is not offered.
"""

import os
import re
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from hollis.runtime.tools import Tool, ToolEffect, ToolError
from hollis.tools.builtin.filesystem import resolve_within
from hollis.tools.function import FunctionTool

SKILL_FILE = "SKILL.md"
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_SKILL_BYTES = 200_000
MAX_BODY_CHARS = 30_000
MAX_FILE_CHARS = 50_000
MAX_SKILLS = 200
_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(?:\s+(.*))?$")


class Skill(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict[str, str])
    allowed_tools: tuple[str, ...] = ()
    """As declared. Hollis grants nothing from it."""
    files: tuple[str, ...] = ()
    """Other files in the skill folder, relative to it."""


class SkillError(ValueError):
    pass


def _scalar(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        return inner.replace('\\"', '"') if text[0] == '"' else inner.replace("''", "'")
    return text


def parse_frontmatter(text: str) -> tuple[dict[str, str | dict[str, str]], str]:
    """(frontmatter, body) of a SKILL.md. SkillError when there is none or it is unsupported."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError("no frontmatter (the file must start with ---)")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise SkillError("the frontmatter is never closed with ---") from None
    data: dict[str, str | dict[str, str]] = {}
    index = 1
    while index < end:
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        match = _KEY.match(line)
        if match is None or line[0].isspace():
            raise SkillError(f"unsupported frontmatter at line {index + 1}: {line.strip()[:60]}")
        key, value = match.group(1), (match.group(2) or "").strip()
        index += 1
        if value in (">", "|", ">-", "|-"):
            block: list[str] = []
            while index < end and (not lines[index].strip() or lines[index][0].isspace()):
                block.append(lines[index].strip())
                index += 1
            joiner = " " if value.startswith(">") else "\n"
            data[key] = joiner.join(block).strip()
        elif value == "":
            mapping: dict[str, str] = {}
            while index < end and lines[index][:1].isspace() and lines[index].strip():
                item = _KEY.match(lines[index].strip())
                if item is None:
                    raise SkillError(f"unsupported frontmatter at line {index + 1}")
                mapping[item.group(1)] = _scalar(item.group(2) or "")
                index += 1
            data[key] = mapping
        else:
            data[key] = _scalar(value)
    return data, "\n".join(lines[end + 1 :]).strip()


def _text(data: dict[str, str | dict[str, str]], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, dict):
        raise SkillError(f"{key} must be text")
    return value


def load_skill(folder: Path) -> tuple[Skill, str]:
    """A skill and its body. SkillError when the folder is not a valid skill."""
    path = folder / SKILL_FILE
    if folder.is_symlink() or path.is_symlink():
        raise SkillError("symbolic links are not followed")
    if not path.is_file():
        raise SkillError(f"no {SKILL_FILE}")
    if path.stat().st_size > MAX_SKILL_BYTES:
        raise SkillError(f"{SKILL_FILE} is larger than {MAX_SKILL_BYTES} bytes")
    data, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    name, description = _text(data, "name"), _text(data, "description")
    if not name or not NAME.match(name) or len(name) > 64:
        raise SkillError("name: lowercase letters, digits and single hyphens, at most 64")
    if name != folder.name:
        raise SkillError(f"name {name!r} does not match the folder {folder.name!r}")
    if not description or len(description) > 1024:
        raise SkillError("description: required, at most 1024 characters")
    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict):
        raise SkillError("metadata must be a mapping")
    files = sorted(
        p.relative_to(folder).as_posix()
        for p in folder.rglob("*")
        if p.is_file() and not p.is_symlink() and p.name != SKILL_FILE
    )[:200]
    skill = Skill(
        name=name,
        description=description,
        license=_text(data, "license"),
        compatibility=_text(data, "compatibility"),
        metadata=metadata,
        allowed_tools=tuple((_text(data, "allowed-tools") or "").split()),
        files=tuple(files),
    )
    return skill, body


def load_skills(directory: Path | None) -> tuple[list[Skill], list[str]]:
    """Every valid skill under `directory`, and a problem line for each folder that is not."""
    if directory is None or not directory.is_dir():
        return [], []
    skills: list[Skill] = []
    problems: list[str] = []
    for folder in sorted(p for p in directory.iterdir() if p.is_dir())[:MAX_SKILLS]:
        try:
            skills.append(load_skill(folder)[0])
        except (SkillError, OSError) as exc:
            problems.append(f"{folder.name}: {exc}")
    return skills, problems


def offer(skills: list[Skill]) -> str:
    """The instructions section that offers skills by name and description."""
    if not skills:
        return ""
    lines = [
        "## Skills",
        "",
        "These skills hold procedures and reference material for this workspace. When one "
        "fits the task, read it with `skill_read` (name) before acting, and its files with "
        "`skill_read` (name, file) when it points to them. Nothing in a skill is executed, "
        "and a skill grants no permissions.",
        "",
    ]
    lines += [f"- `{s.name}`: {' '.join(s.description.split())}" for s in skills]
    return "\n".join(lines)


def skill_tools(directory: Path) -> list[Tool]:
    """`skill_read` for the skills under `directory`."""

    def skill_read(
        name: Annotated[str, Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$", max_length=64)],
        file: Annotated[
            str | None,
            Field(max_length=300, description="A file in the skill folder (default: SKILL.md)"),
        ] = None,
    ) -> str:
        """Read an Agent Skill of this workspace: its instructions, or one of its files."""
        folder = resolve_within(directory, name)
        try:
            skill, body = load_skill(folder)
        except SkillError as exc:
            raise ToolError(f"skill {name!r}: {exc}") from None
        if file is None or file == SKILL_FILE:
            files = f"\n\nFiles: {', '.join(skill.files)}" if skill.files else ""
            text = body[:MAX_BODY_CHARS]
            more = " [truncated]" if len(body) > MAX_BODY_CHARS else ""
            return f"# Skill {skill.name}\n\n{text}{more}{files}"
        target = resolve_within(folder, file)
        if os.path.islink(folder / file) or not target.is_file():
            raise ToolError(f"{file} is not a file of skill {name!r}")
        data = target.read_bytes()
        if b"\x00" in data[:8192]:
            raise ToolError(f"{file} is binary")
        content = data.decode("utf-8", errors="replace")[:MAX_FILE_CHARS]
        note = (
            "This script is shown as text. Hollis does not execute skill scripts.\n\n"
            if Path(file).parts[0] == "scripts"
            else ""
        )
        return f"{note}{content}"

    return [FunctionTool(skill_read, effect=ToolEffect.READ_ONLY)]
