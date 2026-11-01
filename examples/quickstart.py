"""A workspace from scratch, an agent that reads documents and asks before writing.

Runs anywhere, with no model or API key: it uses the built-in demo model (deterministic rules,
not a language model; its answers start with "[demo]").

    uv run python examples/quickstart.py [WORKSPACE_DIR]
"""

import sys
import tempfile
from pathlib import Path

from hollis import Hollis
from hollis.platform.config import initialize
from hollis.platform.schemas import RunPhase


def main(directory: Path) -> None:
    initialize(directory, "quickstart")  # hollis.toml and .hollis/, like `hollis init`
    with Hollis.open(directory) as hollis:
        agent = hollis.agents.starter()  # the `assistant` agent and a `docs` collection
        print(f"agent {agent.name} on {agent.model} with {len(agent.tools)} tools")

        hollis.knowledge.ingest(
            "docs",
            "release.md",
            "# Release process\n\n"
            "Releases ship on Tuesdays. Roll back with `make deploy TAG=<tag>`.\n",
        )
        run = hollis.runs.start("How do we roll back a release?")
        run.wait()
        print("answer:", run.answer())
        grounding = run.detail().grounding
        if grounding is not None:
            supported = sum(1 for claim in grounding.claims if claim.support.value == "supported")
            print(f"grounding: {supported} of {len(grounding.claims)} claims supported")

        write = hollis.runs.start("write notes.txt: tag the release first")
        if write.wait().phase is RunPhase.WAITING_APPROVAL:
            [approval] = write.approvals()
            print(f"approval needed: {approval.tool} {approval.arguments}")
            write.approve(reason="approved by the quickstart script")
        print("write:", write.wait().phase.value, "->", (directory / "notes.txt").read_text())


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(Path(sys.argv[1]))
    else:
        with tempfile.TemporaryDirectory() as temporary:
            main(Path(temporary))
