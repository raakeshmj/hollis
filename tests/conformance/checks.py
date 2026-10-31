"""Protocol conformance checks for a configured deployment.

Each check runs one real Hollis run through the gateway and asserts behaviour Hollis relies on.
Run offline against the loopback server (tests/conformance/test_conformance.py) or against a
real endpoint via the opt-in live configuration. A pass against a live endpoint is evidence of
compatibility for that server version and model only.
"""

from collections.abc import Awaitable, Callable

import pytest

from hollis.adapters.memory_store import InMemoryRunStore
from hollis.gateway import ModelGateway
from hollis.runtime.model import CallOutcome, FailureKind, StopReason, UsageReporting
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import RunSpec
from hollis.runtime.state import RunState, RunStatus

SMALL_LIMIT = 64


async def _run(
    gateway: ModelGateway, model: str, *, instructions: str | None = None, limit: int = 256
) -> RunState:
    runner = gateway.runner(InMemoryRunStore())
    spec = RunSpec(
        model=model,
        input="Reply with the single word: pong",
        instructions=instructions,
        budget=RunBudget(max_output_tokens=limit, max_attempts=1, attempt_timeout_s=120),
    )
    run = await runner.create(spec)
    return await runner.execute(run.run_id)


def _completed(state: RunState) -> None:
    assert state.status is RunStatus.COMPLETED, (state.failure, state.last_failure)
    assert state.response is not None


async def basic_generation(gateway: ModelGateway, model: str) -> None:
    state = await _run(gateway, model)
    _completed(state)
    assert state.response is not None
    assert state.response.content or state.response.stop_reason is StopReason.MAX_TOKENS


async def accepts_instructions(gateway: ModelGateway, model: str) -> None:
    text = gateway.resolve(model).descriptor.capabilities.text
    if text is None or not text.system_prompt:
        pytest.skip("deployment declares no system prompt support")
    _completed(await _run(gateway, model, instructions="You are terse."))


async def respects_output_limit(gateway: ModelGateway, model: str) -> None:
    state = await _run(gateway, model, limit=SMALL_LIMIT)
    _completed(state)
    assert state.response is not None
    if state.response.usage is not None:
        assert state.response.usage.output_tokens <= SMALL_LIMIT


async def reports_usage_as_declared(gateway: ModelGateway, model: str) -> None:
    text = gateway.resolve(model).descriptor.capabilities.text
    declared = text.usage_reporting if text is not None else UsageReporting.NONE
    state = await _run(gateway, model)
    _completed(state)
    assert state.response is not None
    if declared is UsageReporting.NONE:
        return
    assert state.response.usage is not None, f"declared {declared} usage, none reported"
    assert state.response.usage.input_tokens > 0


async def rejects_unknown_model_as_not_processed(gateway: ModelGateway, model: str) -> None:
    """Run against a deployment whose wire model does not exist on the server."""
    state = await _run(gateway, model)
    if state.status is RunStatus.COMPLETED:
        pytest.skip("server ignores the model field (e.g. single-model llama.cpp)")
    assert state.last_failure is not None
    assert state.last_failure.kind is FailureKind.INVALID_REQUEST
    assert state.last_failure.outcome is CallOutcome.NOT_PROCESSED


CHECKS: dict[str, Callable[[ModelGateway, str], Awaitable[None]]] = {
    "basic_generation": basic_generation,
    "accepts_instructions": accepts_instructions,
    "respects_output_limit": respects_output_limit,
    "reports_usage_as_declared": reports_usage_as_declared,
}
