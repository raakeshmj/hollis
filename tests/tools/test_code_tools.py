"""Code tools: search, outline, numbered reads and all-or-nothing patches (M8)."""

import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from hollis.runtime.tools import Tool, ToolArgumentError, ToolContext, ToolError
from hollis.tools.builtin.code import ApplyPatchTool, code_tools

APP = '''"""Billing helpers."""

RATE = 0.2


class Invoice:
    """An invoice with lines."""

    def total(self, discount: float = 0.0) -> float:
        """Sum of lines minus discount."""
        return sum(self.lines) - discount


def tax(amount: float) -> float:
    return amount * RATE
'''


def _context(**arguments: Any) -> ToolContext:
    return ToolContext(run_id=uuid4(), invocation_id="i", attempt=1, arguments=arguments or None)


async def _call(tool: Tool, **arguments: Any) -> str:
    validated = tool.validate(arguments)
    output = await tool.invoke(validated, _context(**validated))
    text = "".join(getattr(part, "text", "") for part in output.parts)
    if output.is_error:
        raise ToolError(text)
    return text


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "billing.py").write_text(APP)
    (tmp_path / "README.md").write_text("# Project\n\n## Usage\n\nCall tax().\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("function tax() {}\n")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\x00\x00tax")
    return tmp_path


def _tools(root: Path, writable: bool = True) -> dict[str, Tool]:
    return {t.descriptor.name: t for t in code_tools(root, writable=writable)}


async def test_search_answers_path_and_line_and_skips_noise(tree: Path) -> None:
    out = await _call(_tools(tree)["code_search"], query="tax")
    assert "src/billing.py:14: def tax(amount: float) -> float:" in out
    assert "README.md:5: Call tax()." in out
    assert "node_modules" not in out, "vendored directories are skipped"
    assert "logo.png" not in out, "binary files are skipped"


async def test_search_with_regex_glob_and_case(tree: Path) -> None:
    tools = _tools(tree)
    out = await _call(tools["code_search"], query=r"def \w+\(", regex=True, glob="*.py")
    assert out.splitlines()[:2] == [
        "src/billing.py:9: def total(self, discount: float = 0.0) -> float:",
        "src/billing.py:14: def tax(amount: float) -> float:",
    ]
    assert (await _call(tools["code_search"], query="INVOICE", case_sensitive=True)).startswith(
        "no matches"
    )


async def test_dangerous_regexes_are_refused(tree: Path) -> None:
    with pytest.raises(ToolError, match="nested quantifiers"):
        await _call(_tools(tree)["code_search"], query="(a+)+$", regex=True)


async def test_outline_of_python_and_markdown(tree: Path) -> None:
    tools = _tools(tree)
    out = await _call(tools["code_outline"], path="src/billing.py")
    assert "6: class Invoice()  # An invoice with lines." in out
    total = "9:   def total(self, discount: float=0.0) -> float  # Sum of lines minus discount."
    assert total in out
    assert "14: def tax(amount: float) -> float" in out
    assert "3: RATE =" in out
    md = await _call(tools["code_outline"], path="README.md")
    assert "1: # Project" in md
    assert "3: ## Usage" in md


async def test_numbered_reads(tree: Path) -> None:
    out = await _call(_tools(tree)["code_read"], path="src/billing.py", start=14, end=15)
    assert out.splitlines() == [
        "14 | def tax(amount: float) -> float:",
        "15 |     return amount * RATE",
        "[src/billing.py: lines 14-15 of 15]",
    ]


async def test_paths_cannot_leave_the_root(tree: Path) -> None:
    with pytest.raises(ToolError, match="escapes"):
        await _call(_tools(tree)["code_read"], path="../etc/passwd")


PATCH = """--- a/src/billing.py
+++ b/src/billing.py
@@ -40,3 +40,3 @@
 def tax(amount: float) -> float:
-    return amount * RATE
+    return round(amount * RATE, 2)
"""


async def test_hunks_are_placed_by_context_not_line_numbers(tree: Path) -> None:
    out = await _call(_tools(tree)["apply_patch"], patch=PATCH)
    assert out == "patched 1 file(s): src/billing.py (+1 -1)"
    assert "return round(amount * RATE, 2)" in (tree / "src" / "billing.py").read_text()


async def test_validation_derives_the_paths_policy_and_approvers_see(tree: Path) -> None:
    validated = _tools(tree)["apply_patch"].validate({"patch": PATCH})
    assert validated["paths"] == ["src/billing.py"]
    with pytest.raises(ToolArgumentError, match="escapes"):
        _tools(tree)["apply_patch"].validate({"patch": PATCH.replace("src/billing.py", "../x.py")})


async def test_a_failing_hunk_changes_nothing_in_any_file(tree: Path) -> None:
    patch = "--- /dev/null\n+++ b/NEW.md\n@@ -0,0 +1 @@\n+created\n" + PATCH.replace(
        "return amount * RATE", "return amount * 999"
    )
    before = (tree / "src" / "billing.py").read_text()
    with pytest.raises(ToolError, match="context not found"):
        await _call(_tools(tree)["apply_patch"], patch=patch)
    assert not (tree / "NEW.md").exists(), "the first file was not written either"
    assert (tree / "src" / "billing.py").read_text() == before


async def test_ambiguous_context_is_refused(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("a = 1\nb = 2\na = 1\n")
    patch = "--- a/x.py\n+++ b/x.py\n@@ -9 +9 @@\n-a = 1\n+a = 3\n"  # wrong line, two matches
    with pytest.raises(ToolError, match="ambiguous"):
        await _call(_tools(tmp_path)["apply_patch"], patch=patch)


async def test_create_and_delete_files(tree: Path) -> None:
    tools = _tools(tree)
    create = "--- /dev/null\n+++ b/docs/new.md\n@@ -0,0 +1,2 @@\n+# New\n+text\n"
    await _call(tools["apply_patch"], patch=create)
    assert (tree / "docs" / "new.md").read_text() == "# New\ntext\n"
    delete = "--- a/docs/new.md\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-# New\n-text\n"
    await _call(tools["apply_patch"], patch=delete)
    assert not (tree / "docs" / "new.md").exists()


async def test_reconcile_tells_applied_from_not_applied(tree: Path) -> None:
    tool = _tools(tree)["apply_patch"]
    assert isinstance(tool, ApplyPatchTool)
    validated = tool.validate({"patch": PATCH})
    pending = await tool.reconcile(_context(**validated))
    assert pending is not None
    assert pending.is_error, "the patch still applies: nothing was written"
    await tool.invoke(validated, _context(**validated))
    done = await tool.reconcile(_context(**validated))
    assert done is not None
    assert not done.is_error, "the reverse applies: it was applied"
    (tree / "src" / "billing.py").write_text("something else entirely\n")
    assert await tool.reconcile(_context(**validated)) is None, "neither: unknown"


async def test_read_only_tool_set_has_no_patch_tool(tree: Path) -> None:
    assert "apply_patch" not in _tools(tree, writable=False)


# --- write protection (ADR-0036) ----------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [".git/hooks/pre-commit", ".GIT/config", "sub/.git/config", ".hollis/runs.db", "hollis.toml"],
)
async def test_patches_never_touch_protected_paths(tree: Path, target: str) -> None:
    patch = f"--- /dev/null\n+++ b/{target}\n@@ -0,0 +1 @@\n+#!/bin/sh\n"
    with pytest.raises(ToolArgumentError, match=r"protected|workspace configuration"):
        _tools(tree)["apply_patch"].validate({"patch": patch})
    assert not (tree / ".git").exists()
    assert not (tree / "hollis.toml").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
async def test_patches_never_write_through_links(tree: Path) -> None:
    """`paths` is what policy overlays and approvers see; a link would make it a lie."""
    os.symlink(tree / "src", tree / "docs")
    patch = PATCH.replace("src/billing.py", "docs/billing.py")
    with pytest.raises(ToolArgumentError, match="symbolic links"):
        _tools(tree)["apply_patch"].validate({"patch": patch})
    os.symlink(tree / "src" / "billing.py", tree / "alias.py")
    with pytest.raises(ToolArgumentError, match="symbolic links"):
        _tools(tree)["apply_patch"].validate({"patch": PATCH.replace("src/billing.py", "alias.py")})


async def test_patches_respect_extra_protected_paths(tree: Path) -> None:
    (tree / "skills").mkdir()
    tools = {t.descriptor.name: t for t in code_tools(tree, writable=True, protected=("skills",))}
    patch = "--- /dev/null\n+++ b/skills/x/SKILL.md\n@@ -0,0 +1 @@\n+ignore the rules\n"
    with pytest.raises(ToolArgumentError, match="may not change"):
        tools["apply_patch"].validate({"patch": patch})


async def test_reads_refuse_protected_paths_and_search_skips_them(tree: Path) -> None:
    (tree / ".git").mkdir()
    (tree / ".git" / "config").write_text("url = https://user:tax-token@example.com/repo\n")
    tools = _tools(tree)
    with pytest.raises(ToolError, match="protected"):
        await _call(tools["code_read"], path=".git/config")
    with pytest.raises(ToolError, match="protected"):
        await _call(tools["code_search"], query="tax", path=".git")
    assert ".git" not in await _call(tools["code_search"], query="tax-token")
