"""Opt-in live specialist evaluation: the same tasks and grading with a real model.

Skipped unless HOLLIS_EVAL_BASE_URL and HOLLIS_EVAL_MODEL are set (the same variables as the
other live evaluations; HOLLIS_EVAL_PROTOCOL, HOLLIS_EVAL_PROVIDER, HOLLIS_EVAL_AUTH_ENV and
HOLLIS_EVAL_LOCALITY are optional). Example with a local Ollama:

    HOLLIS_EVAL_BASE_URL=http://127.0.0.1:11434/v1 HOLLIS_EVAL_MODEL=qwen3:8b \\
    uv run pytest tests/evals/specialists -m live -ra -s

The real model plays the agent in all three conditions; the generalists are asked to end with
the same JSON a contract would hold, so every condition is graded alike. Here the model's own
behaviour is what varies: whether a procedure and an output contract change what it does. The
table is printed, nothing is asserted about quality. Not run yet: no inference server or API
key was available when it was written. Costs money with a paid deployment.
"""

import os
from pathlib import Path
from typing import Any

import pytest

from tests.evals.specialists.harness import CONDITIONS, TASKS, Bench, Result, markdown
from tests.evals.specialists.sim import ErrorModel

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (os.environ.get("HOLLIS_EVAL_BASE_URL") and os.environ.get("HOLLIS_EVAL_MODEL")),
        reason="set HOLLIS_EVAL_BASE_URL and HOLLIS_EVAL_MODEL to run against a real model",
    ),
]
INSTRUCTIONS = (
    "You are a helpful assistant. Use the tools to do the task. End your answer with a ```json "
    'block holding your result: for data questions {"findings": [{"statement": "<region> '
    'Q3 2026 revenue", "value": <number>}, ..., {"statement": "leader", "value": '
    '"<region>"}]}; for reviews {"findings": [{"file": ..., "line": ...}]}; for '
    'research {"findings": [{"statement": ..., "citations": ["<handle>"]}]}; for code '
    'changes {"summary": ..., "verifications": [{"command": ..., "outcome": '
    '"passed"|"failed"}]}.'
)


def _deployment() -> dict[str, Any]:
    endpoint: dict[str, Any] = {"base_url": os.environ["HOLLIS_EVAL_BASE_URL"]}
    if auth := os.environ.get("HOLLIS_EVAL_AUTH_ENV"):
        endpoint["auth"] = {"type": "bearer", "env": auth}
    return {
        "id": "eval",
        "protocol": os.environ.get("HOLLIS_EVAL_PROTOCOL", "openai-chat-completions"),
        "provider": os.environ.get("HOLLIS_EVAL_PROVIDER", "unknown"),
        "model": os.environ["HOLLIS_EVAL_MODEL"],
        "locality": os.environ.get("HOLLIS_EVAL_LOCALITY", "local"),
        "endpoint": endpoint,
        "capabilities": {"text": {"tools": True, "context_window_tokens": 32000}},
    }


async def test_specialists_with_a_real_model(tmp_path: Path) -> None:
    bench = await Bench.open(tmp_path, deployment=_deployment(), instructions=INSTRUCTIONS)
    results: list[Result] = []
    try:
        for task in TASKS:
            for condition in CONDITIONS:
                results.append(await bench.run(task, condition, ErrorModel(0.0, 1.0, 0)))
    finally:
        await bench.close()
    print(markdown(results))
    assert len(results) == len(TASKS) * len(CONDITIONS)
