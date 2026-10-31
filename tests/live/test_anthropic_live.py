"""Opt-in live check against the real Anthropic API, through the gateway.

COSTS REAL MONEY. Runs only when both are set:
    HOLLIS_LIVE_TESTS=1
    ANTHROPIC_API_KEY=<key>
Optional: HOLLIS_LIVE_MODEL (default claude-opus-5-5).

One Messages API request with effort "low" and at most 256 output tokens: a fraction of a cent at
current prices. This verifies live account access and actual provider behaviour; every other
Anthropic test uses a mock transport or a loopback server. For local or self-hosted endpoints, use
the conformance suite (tests/conformance), which needs no API key.

    HOLLIS_LIVE_TESTS=1 uv run pytest tests/live -m live -ra
"""

import os

import pytest

from hollis.adapters.memory_store import InMemoryRunStore
from hollis.gateway import Catalog, ModelGateway
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import RunSpec
from hollis.runtime.state import RunStatus

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("HOLLIS_LIVE_TESTS") != "1" or not os.environ.get("ANTHROPIC_API_KEY"),
        reason="live test: set HOLLIS_LIVE_TESTS=1 and ANTHROPIC_API_KEY (costs money)",
    ),
]


async def test_single_live_run_completes() -> None:
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
                        "text": {"reasoning_effort": ["low", "medium", "high", "xhigh", "max"]}
                    },
                    "options": {"effort": "low", "quota_on_429_without_retry_after": True},
                }
            ]
        }
    )
    gateway = ModelGateway(catalog)
    try:
        runner = gateway.runner(InMemoryRunStore())
        run = await runner.create(
            RunSpec(
                model="live",
                input="Reply with the single word: pong",
                budget=RunBudget(max_output_tokens=256, max_attempts=1, attempt_timeout_s=60),
            )
        )
        final = await runner.execute(run.run_id)
    finally:
        await gateway.aclose()

    assert final.status is RunStatus.COMPLETED, final.failure
    assert final.response is not None
    assert final.response.provider_request_id
    assert final.response.usage is not None
    assert final.response.usage.input_tokens > 0
