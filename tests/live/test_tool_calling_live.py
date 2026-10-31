"""Opt-in live tool-calling round trip against the real Anthropic API (COSTS MONEY).

Runs only with HOLLIS_LIVE_TESTS=1 and ANTHROPIC_API_KEY. Two small requests (effort low,
<= 512 output tokens each). Verifies that a real model calls a Hollis tool and that Hollis can
continue the conversation with the tool result, including the provider's thinking blocks.
"""

import os

import pytest

from hollis.adapters.memory_store import InMemoryRunStore
from hollis.gateway import Catalog, ModelGateway
from hollis.runtime.events import ToolOutcome
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import RunSpec
from hollis.runtime.state import RunStatus
from hollis.tools import ToolRegistry, tool

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("HOLLIS_LIVE_TESTS") != "1" or not os.environ.get("ANTHROPIC_API_KEY"),
        reason="live test: set HOLLIS_LIVE_TESTS=1 and ANTHROPIC_API_KEY (costs money)",
    ),
]


async def test_live_tool_round_trip() -> None:
    registry = ToolRegistry()

    @registry.add
    @tool
    async def get_secret_word() -> str:
        """Returns today's secret word. Call this when asked for the secret word."""
        return "aubergine"

    catalog = Catalog.from_mapping(
        {
            "deployments": [
                {
                    "id": "live",
                    "protocol": "anthropic-messages",
                    "provider": "anthropic",
                    "model": os.environ.get("HOLLIS_LIVE_MODEL", "claude-opus-5-5"),
                    "locality": "external",
                    "endpoint": {
                        "base_url": "https://api.anthropic.com",
                        "auth": {"type": "bearer", "env": "ANTHROPIC_API_KEY"},
                    },
                    "capabilities": {
                        "text": {
                            "tools": True,
                            "reasoning_effort": ["low", "medium", "high", "xhigh", "max"],
                        }
                    },
                    "options": {"effort": "low"},
                }
            ]
        }
    )
    gateway = ModelGateway(catalog)
    try:
        runner = gateway.runner(InMemoryRunStore(), tools=registry)
        run = await runner.create(
            RunSpec(
                model="live",
                input="What is the secret word? Use your tool, then answer in one word.",
                tools=("get_secret_word",),
                budget=RunBudget(max_output_tokens=512, max_attempts=1, max_model_turns=3),
            )
        )
        final = await runner.execute(run.run_id)
    finally:
        await gateway.aclose()

    assert final.status is RunStatus.COMPLETED, final.failure
    outcomes = [i.outcome for t in final.turns for i in t.invocations]
    assert ToolOutcome.SUCCESS in outcomes
    assert final.response is not None
    assert "aubergine" in final.response.content.lower()
