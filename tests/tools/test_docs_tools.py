"""docs_check: Markdown problems a reviewer would catch, as path:line findings."""

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from hollis.runtime.tools import Tool, ToolContext, ToolError
from hollis.tools.builtin.docs import docs_tools, slug

GUIDE = """# Guide

See [setup](setup.md#install), [missing](nope.md), [bad anchor](setup.md#nowhere) and
[here](#usage). External [links](https://example.com) are not checked.

## Usage

![diagram](img/flow.png) and []() and [](setup.md)

#### Skipped level

```bash
# not a heading [x](ignored.md)
```

# Second title
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "root"
    (base / "docs").mkdir(parents=True)
    (base / "docs" / "guide.md").write_text(GUIDE)
    (base / "docs" / "setup.md").write_text("# Setup\n\n## Install\n\nRun it.\n")
    return base


async def _check(root: Path, path: str = ".") -> str:
    tool: Tool = docs_tools(root)[0]
    output = await tool.invoke(
        tool.validate({"path": path}), ToolContext(run_id=uuid4(), invocation_id="i", attempt=1)
    )
    text = "".join(getattr(p, "text", "") for p in output.parts)
    if output.is_error:
        raise ToolError(text)
    return text


async def test_problems_are_reported_with_locations(root: Path) -> None:
    out = await _check(root, "docs/guide.md")
    lines = out.splitlines()
    assert "docs/guide.md:3: broken link: nope.md does not exist" in lines
    assert "docs/guide.md:3: no heading for anchor #nowhere" in lines
    assert "docs/guide.md:8: broken image: img/flow.png does not exist" in lines
    assert "docs/guide.md:8: empty link target" in lines
    assert "docs/guide.md:8: link with no text (setup.md)" in lines
    assert any("heading level jumps from 2 to 4" in line for line in lines)
    assert "docs/guide.md:16: more than one top-level (#) heading" in lines
    assert not any("ignored.md" in line or "example.com" in line for line in lines)
    assert not any("#install" in line or "#usage" in line for line in lines), "valid anchors"
    assert lines[-1] == "1 files checked, 7 problems"


async def test_a_clean_directory_passes(root: Path) -> None:
    (root / "docs" / "guide.md").unlink()
    assert await _check(root, "docs") == "1 files checked, 0 problems: no problems found"


async def test_unclosed_fences_and_links_out_of_the_root(root: Path) -> None:
    (root / "docs" / "open.md").write_text("# Open\n\n[up](../../outside.md)\n\n```\ncode\n")
    (root.parent / "outside.md").write_text("# Outside\n")
    out = await _check(root, "docs/open.md")
    assert "docs/open.md:3: broken link: ../../outside.md does not exist" in out
    assert "docs/open.md:5: code fence is never closed" in out


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
async def test_linked_files_outside_the_root_are_not_read(root: Path) -> None:
    (root.parent / "private.md").write_text("# Private\n\n[secret-name](secret-target.md)\n")
    os.symlink(root.parent / "private.md", root / "docs" / "private.md")
    out = await _check(root, "docs")
    assert "secret" not in out


async def test_protected_paths_are_refused(root: Path) -> None:
    (root / ".git").mkdir()
    with pytest.raises(ToolError, match="protected"):
        await _check(root, ".git")


def test_slugs_follow_github() -> None:
    assert slug("Install `hollis` (macOS)!") == "install-hollis-macos"
    assert slug("[Link](x.md) text") == "link-text"
