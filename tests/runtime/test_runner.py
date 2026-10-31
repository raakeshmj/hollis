import asyncio
import itertools
from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

import pytest

from hollis.adapters.fake_provider import ScriptedModelProvider
from hollis.adapters.memory_store import InMemoryRunStore
from hollis.runtime.events import EventEnvelope, EventType
from hollis.runtime.model import ModelProviderError, ModelRequest, ModelResponse
from hollis.runtime.runner import Runner, RunSpec, operation_id_for
from hollis.runtime.state import RunStatus
from hollis.runtime.store import ConcurrencyConflict, RunStore, load_run
from tests.support.events import T0

SPEC = RunSpec(model="fake", input="hello", instructions="be brief")
REPLY = ModelResponse(content="hi")


def _types(log: Sequence[EventEnvelope]) -> list[EventType]:
    return [e.event_type for e in log]


async def test_happy_path(store: RunStore) -> None:
    provider = ScriptedModelProvider([REPLY])
    runner = Runner(store, provider)
    created = await runner.create(SPEC)
    assert created.status is RunStatus.PENDING

    final = await runner.execute(created.run_id)

    assert final.status is RunStatus.COMPLETED
    assert final.response == REPLY
    assert final == await load_run(store, created.run_id)
    assert _types(await store.read(created.run_id)) == [
        EventType.RUN_CREATED,
        EventType.MODEL_CALL_STARTED,
        EventType.MODEL_RESPONSE_RECORDED,
        EventType.RUN_COMPLETED,
    ]
    assert provider.invocations_by_operation == {operation_id_for(created.run_id): 1}


async def test_request_is_built_from_the_run(store: RunStore) -> None:
    seen: list[ModelRequest] = []

    class Recording(ScriptedModelProvider):
        async def generate(self, request: ModelRequest) -> ModelResponse:
            seen.append(request)
            return await super().generate(request)

    runner = Runner(store, Recording([REPLY]))
    run = await runner.create(SPEC)
    await runner.execute(run.run_id)
    assert seen == [
        ModelRequest(
            operation_id=operation_id_for(run.run_id),
            model="fake",
            input="hello",
            instructions="be brief",
        )
    ]


async def test_injected_clock_and_ids_make_logs_deterministic() -> None:
    async def run_once() -> list[EventEnvelope]:
        ids = (UUID(int=i) for i in itertools.count(1))
        ticks = (T0 + timedelta(seconds=i) for i in itertools.count())
        store = InMemoryRunStore()
        runner = Runner(
            store,
            ScriptedModelProvider([REPLY]),
            clock=lambda: next(ticks),
            new_id=lambda: next(ids),
        )
        run = await runner.create(SPEC)
        await runner.execute(run.run_id)
        return await store.read(run.run_id)

    first, second = await run_once(), await run_once()
    assert first == second
    assert first[0].run_id == UUID(int=1)


async def test_definitive_provider_error_fails_the_run(store: RunStore) -> None:
    provider = ScriptedModelProvider([ModelProviderError("content policy")])
    runner = Runner(store, provider)
    run = await runner.create(SPEC)

    final = await runner.execute(run.run_id)

    assert final.status is RunStatus.FAILED
    assert final.failure is not None
    assert (final.failure.error_type, final.failure.message) == (
        "ModelProviderError",
        "content policy",
    )
    assert final.response is None
    assert (provider.invocations, provider.generations, provider.rejections) == (1, 0, 1)


async def test_unexpected_provider_exception_leaves_run_resumable(store: RunStore) -> None:
    class Flaky(ScriptedModelProvider):
        async def generate(self, request: ModelRequest) -> ModelResponse:
            raise ConnectionError("network dropped")

    run = await Runner(store, Flaky([])).create(SPEC)
    with pytest.raises(ConnectionError):
        await Runner(store, Flaky([])).execute(run.run_id)

    stuck = await load_run(store, run.run_id)
    assert stuck.status is RunStatus.MODEL_CALL_IN_FLIGHT
    assert stuck.attempts == 1

    final = await Runner(store, ScriptedModelProvider([REPLY])).execute(run.run_id)
    assert final.status is RunStatus.COMPLETED
    assert final.attempts == 2


@pytest.mark.parametrize(
    "script", [[REPLY], [ModelProviderError("no")]], ids=["completed", "failed"]
)
async def test_execute_on_terminal_run_is_a_noop(
    store: RunStore, script: list[ModelResponse | ModelProviderError]
) -> None:
    runner = Runner(store, ScriptedModelProvider(script))
    run = await runner.create(SPEC)
    final = await runner.execute(run.run_id)
    log = await store.read(run.run_id)

    untouched = ScriptedModelProvider([])
    assert await Runner(store, untouched).execute(run.run_id) == final
    assert await store.read(run.run_id) == log
    assert untouched.invocations == 0


async def test_runs_are_independent(store: RunStore) -> None:
    runner = Runner(
        store, ScriptedModelProvider([ModelResponse(content="a"), ModelResponse(content="b")])
    )
    first, second = await runner.create(SPEC), await runner.create(SPEC)
    assert first.run_id != second.run_id
    assert (await runner.execute(first.run_id)).response == ModelResponse(content="a")
    assert (await runner.execute(second.run_id)).response == ModelResponse(content="b")


class _BarrierStore:
    """Holds each runner after its read until all have read, so they race on one version."""

    def __init__(self, inner: RunStore, barrier: asyncio.Barrier) -> None:
        self._inner = inner
        self._barrier = barrier

    async def append(
        self, run_id: UUID, expected_version: int, events: Sequence[EventEnvelope]
    ) -> None:
        await self._inner.append(run_id, expected_version, events)

    async def read(self, run_id: UUID) -> list[EventEnvelope]:
        events = await self._inner.read(run_id)
        await self._barrier.wait()
        return events


async def test_racing_runners_record_one_response_and_one_conflicts(store: RunStore) -> None:
    racing = _BarrierStore(store, asyncio.Barrier(2))
    provider = ScriptedModelProvider([REPLY])
    run = await Runner(store, provider).create(SPEC)

    results = await asyncio.gather(
        Runner(racing, provider).execute(run.run_id),
        Runner(racing, provider).execute(run.run_id),
        return_exceptions=True,
    )

    assert sum(isinstance(r, ConcurrencyConflict) for r in results) == 1
    log = await store.read(run.run_id)
    assert _types(log).count(EventType.MODEL_RESPONSE_RECORDED) == 1
    assert _types(log).count(EventType.MODEL_CALL_STARTED) == 1
    assert (await load_run(store, run.run_id)).status is RunStatus.COMPLETED
    # The loser lost its intent append, so it never reached the provider.
    assert provider.invocations == 1
