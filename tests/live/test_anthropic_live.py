"""Opt-in live check against the real Anthropic API.

COSTS REAL MONEY. Runs only when both are set:
    HOLLIS_LIVE_TESTS=1
    ANTHROPIC_API_KEY=<key>
Optional: HOLLIS_LIVE_MODEL (default claude-opus-5-5).

It makes one Messages API request with effort "low" and at most 256 output tokens: a fraction of
a cent at current prices. This is the only test that verifies live account access and actual
provider behaviour; every other adapter test uses a mock transport.

    HOLLIS_LIVE_TESTS=1 uv run pytest tests/live -m live -ra
"""

import os

import pytest

from hollis.adapters.anthropic_provider import AnthropicProvider, AnthropicSettings
from hollis.adapters.credentials import require_env_secret
from hollis.adapters.memory_store import InMemoryRunStore
from hollis.runtime.policy import RunBudget
from hollis.runtime.redaction import Redactor
from hollis.runtime.runner import Runner, RunSpec
from hollis.runtime.state import RunStatus

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("HOLLIS_LIVE_TESTS") != "1" or not os.environ.get("ANTHROPIC_API_KEY"),
        reason="live test: set HOLLIS_LIVE_TESTS=1 and ANTHROPIC_API_KEY (costs money)",
    ),
]


async def test_single_live_run_completes() -> None:
    key = require_env_secret("ANTHROPIC_API_KEY")
    async with AnthropicProvider.from_api_key(key, AnthropicSettings(effort="low")) as provider:
        runner = Runner(InMemoryRunStore(), provider, redactor=Redactor([key]))
        run = await runner.create(
            RunSpec(
                model=os.environ.get("HOLLIS_LIVE_MODEL", "claude-opus-5-5"),
                input="Reply with the single word: pong",
                budget=RunBudget(max_output_tokens=256, max_attempts=1, attempt_timeout_s=60),
            )
        )
        final = await runner.execute(run.run_id)

    assert final.status is RunStatus.COMPLETED, final.failure
    assert final.response is not None
    assert final.response.provider_request_id
    assert final.response.usage is not None
    assert final.response.usage.input_tokens > 0
