"""Print the tables recorded in docs/architecture/orchestration-evaluation.md.

uv run python -m tests.evals.orchestration.report
"""

import asyncio
import tempfile
from pathlib import Path

from tests.evals.orchestration.harness import evaluate, markdown


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        outcomes = await evaluate(Path(directory))
    print(markdown(outcomes))


if __name__ == "__main__":
    asyncio.run(main())
