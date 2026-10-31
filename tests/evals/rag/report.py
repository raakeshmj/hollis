"""Print the retrieval and grounding tables recorded in docs/architecture/rag-evaluation.md.

uv run python -m tests.evals.rag.report
"""

import asyncio
import tempfile
from collections import Counter
from pathlib import Path

from hollis.runtime.grounding import check
from tests.evals.rag.corpus import clinical_ops, engineering
from tests.evals.rag.grounding_cases import CASES, EVIDENCE
from tests.evals.rag.harness import evaluate, markdown


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        for corpus in (engineering(), clinical_ops()):
            print(markdown(await evaluate(corpus, Path(directory))))
            print()
    confusion: Counter[tuple[str, str]] = Counter()
    for answer, expected, _ in CASES:
        got = [c.support.value for c in check(answer, EVIDENCE).claims]
        for index in range(max(len(got), len(expected))):
            confusion[
                (
                    expected[index].value if index < len(expected) else "-",
                    got[index] if index < len(got) else "-",
                )
            ] += 1
    agreed = sum(n for (e, g), n in confusion.items() if e == g)
    print(
        f"#### Grounding verifier: {agreed}/{sum(confusion.values())} claims "
        "classified as labelled\n"
    )
    print("| expected | got | count |\n|---|---|---|")
    for (expected, got), count in sorted(confusion.items()):
        print(f"| {expected} | {got} | {count} |")


if __name__ == "__main__":
    asyncio.run(main())
