"""Templates and capabilities: loading, materialization, parameters, readiness; Agent Skills."""

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from hollis.runtime.tools import Decision, ToolContext, ToolEffect, ToolError
from hollis.specialists import (
    CONTRACTS,
    Catalog,
    ModelFacts,
    TemplateError,
    WorkspaceFacts,
    load_skills,
    materialize,
    model_checks,
    offer,
    parameter_values,
    skill_tools,
    workspace_checks,
)
from hollis.specialists.skills import SkillError, parse_frontmatter

FIRST_PARTY = {
    "software-engineer",
    "code-reviewer",
    "researcher",
    "data-analyst",
    "technical-writer",
    "automation",
    "coordinator",
}
ALL_TOOLS = frozenset(
    {
        "code_search",
        "code_outline",
        "code_read",
        "apply_patch",
        "fs_write_file",
        "fs_read_file",
        "fs_list_dir",
        "docs_check",
        "data_list",
        "data_describe",
        "data_query",
        "knowledge_search",
        "knowledge_read",
        "knowledge_list",
        "knowledge_outline",
        "git_diff",
        "run_tests",
    }
)


def test_first_party_templates_load_and_name_no_vendor() -> None:
    catalog = Catalog.load()
    assert catalog.problems == []
    assert {e.template.id for e in catalog.templates()} == FIRST_PARTY
    for entry in catalog.templates():
        text = entry.template.model_dump_json().lower()
        for vendor in ("anthropic", "openai", "claude", "gpt", "ollama", "gemini", "llama"):
            assert vendor not in text, (entry.template.id, vendor)
        assert entry.template.contract in CONTRACTS
        assert entry.source == "builtin"
        assert entry.digest.startswith("sha256:")


def test_materialization_builds_an_ordinary_agent() -> None:
    entry = Catalog.load().entry("software-engineer")
    draft = materialize(
        entry,
        parameter_values(entry.template, {"conventions": "Use type hints."}),
        available=ALL_TOOLS,
        commands=("run_tests", "not_configured"),
    )
    assert draft.tools[:4] == ("code_search", "code_outline", "code_read", "apply_patch")
    assert "git_diff" in draft.tools, "optional tools the workspace has"
    assert "run_tests" in draft.tools, "configured commands"
    assert "not_configured" not in draft.tools
    assert "Use type hints." in draft.instructions
    assert "## Code editing" in draft.instructions, "capability guidance follows the procedure"
    assert draft.contract == "change-report"
    assert draft.missing == ()


def test_overlays_come_from_capabilities_and_template_with_parameters_filled() -> None:
    entry = Catalog.load().entry("technical-writer")
    draft = materialize(entry, {"docs_dir": "manual"}, available=ALL_TOOLS)
    docs_only = next(rule for rule in draft.policy if rule.name == "docs-only")
    assert docs_only.decision is Decision.DENY
    assert docs_only.outside == {"path": ("manual/*",), "paths": ("manual/*",)}
    reviewer = materialize(Catalog.load().entry("code-reviewer"), {}, available=ALL_TOOLS)
    read_only = next(rule for rule in reviewer.policy if rule.name == "read-only")
    assert set(read_only.effects) == {ToolEffect.MUTATING, ToolEffect.DESTRUCTIVE}
    assert "apply_patch" not in reviewer.tools


def test_parameters_are_validated() -> None:
    template = Catalog.load().entry("technical-writer").template
    assert parameter_values(template, {}) == {"docs_dir": "docs"}
    for given, problem in [
        ({"other": "x"}, "unknown parameters"),
        ({"docs_dir": "../etc"}, "must match"),
        ({"docs_dir": "x" * 600}, "at most 500"),
    ]:
        with pytest.raises(TemplateError, match=problem):
            parameter_values(template, given)


def test_readiness_reports_hard_and_soft_requirements() -> None:
    entry = Catalog.load().entry("software-engineer")
    bare = WorkspaceFacts(
        tools=frozenset({"code_search"}),
        filesystem=True,
        writable=False,
        git=False,
        commands=(),
        collections=(),
        agents=(),
    )
    draft = materialize(entry, {"conventions": ""}, available=bare.tools)
    checks = {c.requirement: c for c in workspace_checks(entry, draft, bare)}
    assert checks["filesystem"].ok
    assert not checks["writable files"].ok
    assert checks["writable files"].hard
    assert not checks["commands"].ok
    assert not checks["commands"].hard
    assert not checks["tool apply_patch"].ok
    assert checks["tool apply_patch"].hard

    fit = {c.requirement: c for c in model_checks(entry, ModelFacts("tiny", False, 8000))}
    assert not fit["tool calling"].ok
    assert fit["tool calling"].hard
    assert not fit["context window"].ok
    assert fit["context window"].hard
    unknown = {c.requirement: c for c in model_checks(entry, ModelFacts("local", True, None))}
    assert not unknown["context window"].ok
    assert not unknown["context window"].hard


def test_workspace_templates_load_and_cannot_take_first_party_ids(tmp_path: Path) -> None:
    (tmp_path / "triage.toml").write_text(
        'id = "triage"\nversion = "1.2.0"\nname = "Triage"\ndescription = "Sorts issues."\n'
        'instructions = "Sort {queue}."\ncapabilities = ["code-reading"]\n'
        '[[parameters]]\nname = "queue"\ndescription = "Queue"\ndefault = "inbox"\n'
    )
    (tmp_path / "researcher.toml").write_text(
        'id = "researcher"\nversion = "9.0.0"\nname = "R"\ndescription = "d"\ninstructions = "i"\n'
    )
    (tmp_path / "broken.toml").write_text('id = "x"\n')
    (tmp_path / "unknown-cap.toml").write_text(
        'id = "u"\nversion = "1.0.0"\nname = "U"\ndescription = "d"\ninstructions = "i"\n'
        'capabilities = ["flying"]\n'
    )
    (tmp_path / "undeclared.toml").write_text(
        'id = "v"\nversion = "1.0.0"\nname = "V"\ndescription = "d"\ninstructions = "Use {x}."\n'
    )
    catalog = Catalog.load(tmp_path)
    assert catalog.entry("triage").source == "workspace:triage.toml"
    assert catalog.entry("researcher").source == "builtin"
    problems = "\n".join(catalog.problems)
    assert "already taken" in problems
    assert "broken.toml" in problems
    assert "unknown capabilities: flying" in problems
    assert "placeholders without a parameter" in problems


# --- Agent Skills -------------------------------------------------------------------------------

SKILL = """---
name: release-notes
description: >
  Write release notes from merged changes,
  grouped by area.
license: Apache-2.0
allowed-tools: Bash(git:*) Read
metadata:
  author: platform-team
  version: "1.0"
---
# Release notes

Group changes by area. See references/style.md.
"""


@pytest.fixture
def skills(tmp_path: Path) -> Path:
    folder = tmp_path / "skills" / "release-notes"
    (folder / "references").mkdir(parents=True)
    (folder / "scripts").mkdir()
    (folder / "SKILL.md").write_text(SKILL)
    (folder / "references" / "style.md").write_text("Use the past tense.\n")
    marker = tmp_path / "EXECUTED"
    (folder / "scripts" / "collect.sh").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (folder / "scripts" / "collect.sh").chmod(0o755)
    bad = tmp_path / "skills" / "Bad_Name"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: Bad_Name\ndescription: x\n---\n")
    mismatch = tmp_path / "skills" / "other"
    mismatch.mkdir()
    (mismatch / "SKILL.md").write_text("---\nname: something-else\ndescription: x\n---\n")
    return tmp_path / "skills"


def test_skills_load_with_their_frontmatter(skills: Path) -> None:
    loaded, problems = load_skills(skills)
    assert [s.name for s in loaded] == ["release-notes"]
    skill = loaded[0]
    assert skill.description == "Write release notes from merged changes, grouped by area."
    assert skill.metadata == {"author": "platform-team", "version": "1.0"}
    assert skill.allowed_tools == ("Bash(git:*)", "Read"), "shown as declared, granting nothing"
    assert skill.files == ("references/style.md", "scripts/collect.sh")
    assert any("Bad_Name" in p for p in problems)
    assert any("does not match the folder" in p for p in problems)
    section = offer(loaded)
    assert "`release-notes`: Write release notes" in section
    assert "grants no permissions" in section


async def test_skill_read_shows_text_and_never_executes(skills: Path) -> None:
    tool = skill_tools(skills)[0]
    context = ToolContext(run_id=uuid4(), invocation_id="i", attempt=1)

    async def read(**arguments: str) -> str:
        output = await tool.invoke(tool.validate(arguments), context)
        text = "".join(getattr(p, "text", "") for p in output.parts)
        if output.is_error:
            raise ToolError(text)
        return text

    body = await read(name="release-notes")
    assert body.startswith("# Skill release-notes")
    assert "Group changes by area." in body
    assert "name: release-notes" not in body, "frontmatter is not repeated"
    assert await read(name="release-notes", file="references/style.md") == "Use the past tense.\n"
    script = await read(name="release-notes", file="scripts/collect.sh")
    assert script.startswith("This script is shown as text. Hollis does not execute")
    assert not (skills.parent / "EXECUTED").exists()
    for name, file, problem in [
        ("release-notes", "../other/SKILL.md", "escapes"),
        ("other", None, "does not match"),
        ("missing", None, "no SKILL.md"),
    ]:
        with pytest.raises(ToolError, match=problem):
            await read(name=name, **({"file": file} if file else {}))
    assert tool.descriptor.effect is ToolEffect.READ_ONLY


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_linked_skills_are_not_loaded(skills: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-skill"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: outside-skill\ndescription: x\n---\n")
    os.symlink(outside, skills / "outside-skill")
    loaded, problems = load_skills(skills)
    assert "outside-skill" not in [s.name for s in loaded]
    assert any("symbolic links" in p for p in problems)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("# no frontmatter", "no frontmatter"),
        ("---\nname: x\n", "never closed"),
        ("---\nname: x\n- item\n---\n", "unsupported frontmatter"),
    ],
)
def test_unsupported_frontmatter_is_reported(text: str, problem: str) -> None:
    with pytest.raises(SkillError, match=problem):
        parse_frontmatter(text)
