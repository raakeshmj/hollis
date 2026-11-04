"""Specialized agents from templates: a data analyst and a read-only code reviewer, their checked
outputs, a customization, and an export as a template file.

Runs anywhere, with no model or API key: the agents use the built-in demo model (deterministic
rules, not a language model), which follows each template's procedure minimally and honestly.

    uv run python examples/specialists.py [WORKSPACE_DIR]
"""

import sys
import tempfile
from pathlib import Path

from hollis import Hollis
from hollis.platform.config import initialize

SALES = "region,amount\nnorth,120.50\nsouth,98.00\nnorth,40.25\n"
CODE = (
    "def page(items, number, size=20):\n"
    "    start = (number - 1) * size\n"
    "    return items[start:start + size - 1]\n"
)


def main(directory: Path) -> None:
    initialize(directory, "specialists")
    (directory / "sales.csv").write_text(SALES)
    (directory / "pagination.py").write_text(CODE)
    with Hollis.open(directory) as hollis:
        for template in hollis.templates.list():
            print(f"template {template.id}: {'ready' if template.ready else 'not ready'}")

        hollis.agents.create_from_template("data-analyst", "analyst", "demo")
        run = hollis.runs.start("How many sales are there?", agent="analyst")
        run.wait()
        output = run.output()
        if output is None:
            raise SystemExit("the analyst has no output contract")
        print(f"analyst output: {output.status} {output.data and output.data['findings']}")

        reviewer = hollis.agents.create_from_template(
            "code-reviewer", "reviewer", "demo", parameters={"focus": "off-by-one errors"}
        )
        print(f"reviewer policy: {[rule.name for rule in reviewer.policy]}")
        run = hollis.runs.start("Review pagination.py", agent="reviewer")
        run.wait()
        output = run.output()
        if output is None:
            raise SystemExit("the reviewer has no output contract")
        print(f"review output: {output.status}, scope {output.data and output.data['scope']}")

        customized = hollis.agents.customize("reviewer", description="Reviews our pagination.")
        origin = customized.origin
        print(f"customized: {origin.overrides if origin else 'not from a template'}")
        preview = hollis.agents.upgrade_preview("reviewer")
        print(f"up to date with {preview.template} {preview.to_version}: {preview.up_to_date}")

        exported = hollis.agents.export_template("reviewer", id="pagination-review")
        target = directory / "agent-templates" / exported.filename
        target.parent.mkdir(exist_ok=True)
        target.write_text(exported.content)
        shared = [t.id for t in hollis.templates.list() if t.source.startswith("workspace")]
        print(f"workspace templates: {shared}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(Path(sys.argv[1]))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            main(Path(tmp))
