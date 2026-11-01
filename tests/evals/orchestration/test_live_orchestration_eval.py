"""Opt-in live orchestration evaluation: the breadth scenario with a real model in every role.

Skipped unless HOLLIS_EVAL_BASE_URL and HOLLIS_EVAL_MODEL are set (same variables as the other
live evaluations; HOLLIS_EVAL_PROTOCOL, HOLLIS_EVAL_PROVIDER, HOLLIS_EVAL_AUTH_ENV and
HOLLIS_EVAL_LOCALITY are optional). Example with a local Ollama:

    HOLLIS_EVAL_BASE_URL=http://127.0.0.1:11434/v1 HOLLIS_EVAL_MODEL=qwen3:8b \\
    uv run pytest tests/evals/orchestration -m live -ra -s

The model plays the single agent, the lead and the specialists; answers are graded by the same
fact phrases as the offline evaluation and the table is printed. Not run yet: no inference
server or API key was available when it was written. Costs money with a paid deployment.
"""

import os
from pathlib import Path
from typing import Any

import pytest

from tests.evals.orchestration import corpus, harness
from tests.evals.orchestration.corpus import BREADTH, DESCRIPTIONS

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (os.environ.get("HOLLIS_EVAL_BASE_URL") and os.environ.get("HOLLIS_EVAL_MODEL")),
        reason="set HOLLIS_EVAL_BASE_URL and HOLLIS_EVAL_MODEL to run against a real model",
    ),
]
INSTRUCTIONS = (
    "Answer each question with the knowledge_search tool. Quote the sentence that answers it "
    "and cite its handle, e.g. [C-1a2b3c4d]. If the sources do not answer, say so."
)
LEAD = (
    "You coordinate specialists. Delegate each question to the specialist whose description "
    "fits it (one delegation per specialist, all at once), then combine their answers, keeping "
    "their citations. Say which questions were not answered."
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


async def test_breadth_with_a_real_model(tmp_path: Path) -> None:
    bench = await harness.Bench.open(tmp_path, [], {d: corpus.document(d) for d in harness.DOMAINS})
    platform, admin = bench.platform, bench.admin
    from hollis.platform.schemas import AgentInput, DeploymentInput
    from hollis.runtime.knowledge import KnowledgeAccess
    from hollis.runtime.policy import RunBudget

    await platform.upsert_deployment(admin, DeploymentInput(deployment=_deployment()))
    budget = RunBudget(max_output_tokens=2048, max_model_turns=16)

    async def agent(
        name: str, instructions: str, collections: tuple[str, ...], **extra: Any
    ) -> None:
        await platform.create_agent(
            admin,
            AgentInput(
                name=name,
                model="eval",
                instructions=instructions,
                tools=("knowledge_search",) if collections else (),
                knowledge=KnowledgeAccess(collections=collections),
                context=harness.NO_START_RETRIEVAL,
                budget=budget,
                **extra,
            ),
        )

    for domain in harness.DOMAINS:
        await agent(
            f"{domain}-specialist", INSTRUCTIONS, (domain,), description=DESCRIPTIONS[domain]
        )
    await agent("generalist", INSTRUCTIONS, harness.DOMAINS)
    await agent(
        "lead", LEAD, harness.DOMAINS, delegates=tuple(harness.SPECIALISTS.values()), tools=()
    )
    task = harness.breadth_task(BREADTH)
    try:
        outcomes = [
            await bench.run(
                "live", "single agent", task, BREADTH, agent="generalist", timeout_s=900
            ),
            await bench.run(
                "live", "lead + 4 specialists", task, BREADTH, agent="lead", timeout_s=900
            ),
            await bench.run(
                "live",
                "plan: 4 specialists",
                task,
                BREADTH,
                plan=harness.specialists_plan(BREADTH, harness.SPECIALISTS),
                timeout_s=900,
            ),
        ]
    finally:
        await bench.close()
    print("\n" + harness.markdown(outcomes))
    assert all(o.phase in ("completed", "failed") for o in outcomes)
