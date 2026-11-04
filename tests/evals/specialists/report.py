"""Print the tables recorded in docs/architecture/specialist-evaluation.md.

uv run python -m tests.evals.specialists.report
"""

import asyncio
import tempfile
from pathlib import Path

from tests.evals.specialists.harness import evaluate, markdown


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        results = await evaluate(Path(directory))
    print(markdown(results))


if __name__ == "__main__":
    asyncio.run(main())
