"""Crash recovery for a gateway-resolved local OpenAI-compatible deployment."""

from typing import Any

import pytest

from hollis.adapters.config import EndpointConfig
from hollis.gateway import Catalog, ModelGateway
from hollis.runtime.events import EventType, FailureReason
from hollis.runtime.model import FailureKind
from hollis.runtime.policy import AmbiguousOutcomePolicy, RunBudget
from hollis.runtime.runner import Runner, RunSpec
from hollis.runtime.state import RunStatus
from hollis.runtime.store import RunStore
from tests.support import openai_chat
from tests.support.crash import CrashAfterGenerate, CrashingStore, SimulatedCrash
from tests.support.mock_http import MockHTTP

CATALOG = Catalog.from_mapping(
    {
        "deployments": [
            {
                "id": "local",
                "protocol": "openai-chat-completions",
                "provider": "ollama",
                "model": "qwen3:8b",
                "locality": "local",
                "endpoint": {"base_url": "http://127.0.0.1:11434/v1"},
            }
        ]
    }
)


def _gateway(server: MockHTTP) -> ModelGateway:
    def factory(endpoint: EndpointConfig) -> Any:
        return server.http_client()

    return ModelGateway(CATALOG, http_client_factory=factory)


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.parametrize(
    ("policy", "status", "calls"),
    [(None, RunStatus.FAILED, 1), (AmbiguousOutcomePolicy.RETRY, RunStatus.COMPLETED, 2)],
)
async def test_crash_after_generation(
    store: RunStore, policy: AmbiguousOutcomePolicy | None, status: RunStatus, calls: int
) -> None:
    server = MockHTTP(openai_chat.ok(), openai_chat.ok())
    gateway = _gateway(server)
    provider = gateway.resolve("local")
    crashing = Runner(CrashingStore(store), CrashAfterGenerate(provider), sleep=_no_sleep)
    spec = RunSpec(
        model="local", input="hi", budget=RunBudget(max_output_tokens=64), ambiguous_policy=policy
    )
    run = await crashing.create(spec)
    with pytest.raises(SimulatedCrash):
        await crashing.execute(run.run_id)

    final = await gateway.runner(store, sleep=_no_sleep).execute(run.run_id)

    assert (final.status, server.calls) == (status, calls)
    if status is RunStatus.FAILED:
        assert final.failure is not None
        assert (final.failure.reason, final.failure.kind) == (
            FailureReason.AMBIGUOUS_OUTCOME,
            FailureKind.INTERRUPTED,
        )


async def test_recorded_response_is_never_requested_again(store: RunStore) -> None:
    server = MockHTTP(openai_chat.ok())
    gateway = _gateway(server)
    crashing = Runner(
        CrashingStore(store, EventType.MODEL_RESPONSE_RECORDED), gateway.resolve("local")
    )
    run = await crashing.create(
        RunSpec(model="local", input="hi", budget=RunBudget(max_output_tokens=64))
    )
    with pytest.raises(SimulatedCrash):
        await crashing.execute(run.run_id)

    final = await gateway.runner(store).execute(run.run_id)
    assert (final.status, server.calls) == (RunStatus.COMPLETED, 1)
