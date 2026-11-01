"""Agents that delegate: a lead with two specialists and a reviewer, then the same work as a plan.

Runs anywhere, with no model or API key: every agent uses the built-in demo model (deterministic
rules, not a language model). With the demo model, "@agent" in a task delegates to that agent,
and "then" waits for the earlier results and passes them along.

    uv run python examples/orchestration.py [WORKSPACE_DIR]
"""

import sys
import tempfile
from pathlib import Path

from hollis import Hollis, Plan, PlanStep
from hollis.platform.config import initialize
from hollis.runtime.knowledge import KnowledgeAccess

HANDBOOK = "# Handbook\n\n## Deploying\n\nReleases are deployed with make ship from main.\n"
FINANCE = "# Finance\n\n## Travel\n\nThe travel budget is 41000 euros per team for 2026.\n"


def main(directory: Path) -> None:
    initialize(directory, "orchestration")
    with Hollis.open(directory) as hollis:
        for name, text in (("docs", HANDBOOK), ("finance", FINANCE)):
            hollis.knowledge.create_collection(name)
            hollis.knowledge.ingest(name, f"{name}.md", text)

        def specialist(name: str, collection: str, about: str) -> None:
            hollis.agents.create(
                name=name,
                model="demo",
                description=about,
                tools=("knowledge_search",),
                knowledge=KnowledgeAccess(collections=(collection,)),
            )

        specialist("researcher", "docs", "answers from the engineering handbook")
        specialist("analyst", "finance", "answers from the finance handbook")
        hollis.agents.create(name="reviewer", model="demo", description="checks reports")
        hollis.agents.create(
            name="lead",
            model="demo",
            delegates=("researcher", "analyst", "reviewer"),
            # A lead's children can read only what the lead can: give it both collections.
            knowledge=KnowledgeAccess(collections=("docs", "finance")),
        )

        run = hollis.runs.start(
            "@researcher how do we deploy? @analyst what is the travel budget? "
            "then @reviewer check both answers",
            agent="lead",
        )
        run.wait()
        tree = run.tree()
        print(f"lead: {run.phase.value}, {tree.totals.runs} runs, depth {tree.totals.max_depth}")
        for node in tree.nodes[1:]:
            report = node.report
            checked = report.verification if report else None
            supported = f"{checked.supported}/{checked.claims} supported" if checked else "-"
            print(f"  {node.run.agent}: {node.run.phase.value}, {supported}")
        print(run.answer())

        plan = Plan(
            steps=(
                PlanStep(id="deploy", agent="researcher", objective="how do we deploy?"),
                PlanStep(id="budget", agent="analyst", objective="what is the travel budget?"),
                PlanStep(
                    id="review",
                    agent="reviewer",
                    objective="check both answers",
                    depends_on=("deploy", "budget"),
                ),
            )
        )
        planned = hollis.runs.start("Release brief", plan=plan)
        planned.wait()
        print("plan:", (planned.answer() or "").splitlines()[0])


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(Path(sys.argv[1]))
    else:
        with tempfile.TemporaryDirectory() as temporary:
            main(Path(temporary))
