"""Print the evaluation tables recorded in docs/architecture/evaluation.md.

uv run python -m tests.evals.report
"""

import asyncio
import tempfile
from pathlib import Path

from tests.evals import compaction, harness
from tests.evals.scenario import build, build_research


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        reports = [
            await harness.evaluate(
                build(noise_per_session=10), store_kind="sqlite", directory=root / "a"
            ),
            await harness.evaluate(
                build(noise_per_session=40), store_kind="sqlite", directory=root / "b"
            ),
            await harness.evaluate(
                build_research(noise_per_session=40), store_kind="sqlite", directory=root / "c"
            ),
        ]
    print(harness.markdown(reports))
    deterministic = await compaction.run_compaction("deterministic")
    forgetful = await compaction.run_compaction("forgetful")
    naive = compaction.naive_replace_notes_kept(forgetful.drafts)
    print("### Compaction fidelity\n")
    print(compaction.markdown([deterministic, forgetful], naive))


if __name__ == "__main__":
    asyncio.run(main())
