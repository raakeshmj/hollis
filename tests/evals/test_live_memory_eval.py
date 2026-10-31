"""Opt-in live evaluation: a real model answers the probes with each condition's context.

Skipped unless HOLLIS_EVAL_BASE_URL and HOLLIS_EVAL_MODEL are set. Uses the gateway, so any
supported protocol and server works (local, private or external). Example with a local server:

    HOLLIS_EVAL_BASE_URL=http://127.0.0.1:11434/v1 HOLLIS_EVAL_MODEL=qwen3:8b \\
    HOLLIS_EVAL_PROVIDER=ollama uv run pytest tests/evals -m live -ra -s

Optional: HOLLIS_EVAL_PROTOCOL (default openai-chat-completions), HOLLIS_EVAL_AUTH_ENV (name of
the variable holding a bearer token). External endpoints cost money: 4 conditions x 12 probes.

Grading is keyword-based (`Probe.answer`) and abstention probes pass when the model declines.
This is a coarse check intended to show whether context sufficiency translates into answers;
it has not been run (no inference server was available when it was written).
"""

import os
from typing import Any

import pytest

from hollis.gateway import Catalog, ModelGateway
from hollis.memory import InMemoryMemoryStore, MemoryManager, RecallQuery
from hollis.runtime.context import CHARS_PER_TOKEN
from hollis.runtime.model import ModelRequest
from tests.evals.harness import ALICE, CONDITIONS, EvalClock, history_lines, ingest, window
from tests.evals.scenario import build, probe_time

pytestmark = pytest.mark.live

INSTRUCTIONS = (
    "Answer the question using only the notes provided. If the notes do not contain the answer, "
    "say you don't know. Answer in one sentence."
)
DECLINES = (
    "don't know",
    "do not know",
    "not in the notes",
    "no information",
    "not mentioned",
    "cannot",
)


def _catalog() -> Catalog:
    endpoint: dict[str, Any] = {"base_url": os.environ["HOLLIS_EVAL_BASE_URL"]}
    if auth := os.environ.get("HOLLIS_EVAL_AUTH_ENV"):
        endpoint["auth"] = {"type": "bearer", "env": auth}
    return Catalog.from_mapping(
        {
            "deployments": [
                {
                    "id": "eval",
                    "protocol": os.environ.get("HOLLIS_EVAL_PROTOCOL", "openai-chat-completions"),
                    "provider": os.environ.get("HOLLIS_EVAL_PROVIDER", "unknown"),
                    "model": os.environ["HOLLIS_EVAL_MODEL"],
                    "locality": os.environ.get("HOLLIS_EVAL_LOCALITY", "local"),
                    "endpoint": endpoint,
                }
            ]
        }
    )


@pytest.mark.skipif(
    not (os.environ.get("HOLLIS_EVAL_BASE_URL") and os.environ.get("HOLLIS_EVAL_MODEL")),
    reason="set HOLLIS_EVAL_BASE_URL and HOLLIS_EVAL_MODEL to run the live memory evaluation",
)
async def test_live_answers_by_condition() -> None:
    scenario = build(noise_per_session=40)
    gateway = ModelGateway(_catalog())
    provider = gateway.resolve("eval")
    budget = 1200
    correct: dict[str, int] = dict.fromkeys(CONDITIONS, 0)
    try:
        for probe in scenario.probes:
            clock = EvalClock(scenario.statements[0].at)
            manager = MemoryManager(InMemoryMemoryStore(), clock=clock)
            until = probe.session if probe.session is not None else scenario.sessions
            await ingest(scenario, manager, clock, until)
            clock.now = probe_time(scenario, probe)
            recalled = await manager.recall(
                RecallQuery(
                    text=probe.question, scopes=ALICE.read, max_chars=int(budget * CHARS_PER_TOKEN)
                )
            )
            contexts = {
                "no_memory": "",
                "context_only": window(history_lines(scenario, probe, current_only=True), budget),
                "raw_history": window(history_lines(scenario, probe, current_only=False), budget),
                "memory": "\n".join(m.render() for m in recalled),
            }
            for condition, notes in contexts.items():
                response = await provider.generate(
                    ModelRequest(
                        operation_id=f"eval/{probe.pid}/{condition}",
                        model=provider.descriptor.model,
                        instructions=INSTRUCTIONS,
                        input=f"Notes:\n{notes or '(none)'}\n\nQuestion: {probe.question}",
                        max_output_tokens=200,
                        timeout_s=120,
                    )
                )
                answer = response.content.casefold()
                ok = (
                    all(k in answer for k in probe.answer)
                    if probe.answer
                    else any(d in answer for d in DECLINES)
                )
                correct[condition] += ok
                print(f"{probe.pid:12} {condition:13} {'OK ' if ok else 'MISS'} {answer[:100]!r}")
    finally:
        await gateway.aclose()
    total = len(scenario.probes)
    print({c: f"{n}/{total}" for c, n in correct.items()})
    assert correct["memory"] >= correct["raw_history"]
