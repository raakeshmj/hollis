import asyncio
import itertools
from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

import pytest

from hollis.adapters.fake_provider import FAKE_DESCRIPTOR, ScriptedModelProvider
from hollis.adapters.memory_store import InMemoryRunStore
from hollis.runtime.events import (
    EventEnvelope,
    EventType,
    FailureReason,
    ModelCallFailed,
)
from hollis.runtime.model import (
    CapabilityMismatch,
    FailureKind,
    Locality,
    ModelCallError,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    TextCapabilities,
    UnknownModel,
    Usage,
)
from hollis.runtime.policy import AmbiguousOutcomePolicy, RunBudget
from hollis.runtime.runner import DeploymentChanged, Runner, RunSpec, operation_id_for
from hollis.runtime.state import RunStatus
from hollis.runtime.store import ConcurrencyConflict, RunStore, load_run
from tests.support.events import T0

BUDGET = RunBudget(max_output_tokens=64, max_attempts=3, attempt_timeout_s=30)
SPEC = RunSpec(model="fake", input="hello", instructions="be brief", budget=BUDGET)
REPLY = ModelResponse(content="hi", usage=Usage(input_tokens=5, output_tokens=2))
NON_DEDUP = FAKE_DESCRIPTOR.model_copy(update={"deduplicates_requests": False})


class FakeTime:
    """A clock whose sleep advances time instantly and records the requested delays."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


def _runner(
    store: RunStore, provider: ScriptedModelProvider, time: FakeTime | None = None
) -> Runner:
    time = time or FakeTime()
    return Runner(store, provider, clock=time.clock, sleep=time.sleep, jitter=lambda: 1.0)


def _types(log: Sequence[EventEnvelope]) -> list[EventType]:
    return [e.event_type for e in log]


def _error(
    kind: FailureKind,
    *,
    retry_after_s: float | None = None,
    provider_request_id: str | None = None,
) -> ModelCallError:
    return ModelCallError(
        kind,
        f"{kind} happened",
        retry_after_s=retry_after_s,
        provider_request_id=provider_request_id,
    )


async def _run(runner: Runner, spec: RunSpec = SPEC) -> UUID:
    run = await runner.create(spec)
    await runner.execute(run.run_id)
    return run.run_id


# --- happy path and request construction -------------------------------------------------


async def test_happy_path(store: RunStore) -> None:
    provider = ScriptedModelProvider([REPLY])
    runner = _runner(store, provider)
    created = await runner.create(SPEC)
    assert created.status is RunStatus.PENDING
    assert created.deployment == FAKE_DESCRIPTOR.ref

    final = await runner.execute(created.run_id)

    assert final.status is RunStatus.COMPLETED
    assert final.response == REPLY
    assert final.usage.model_dump() == {"input_tokens": 5, "output_tokens": 2}
    assert final == await load_run(store, created.run_id)
    assert _types(await store.read(created.run_id)) == [
        EventType.RUN_CREATED,
        EventType.CONTEXT_ASSEMBLED,
        EventType.MODEL_CALL_STARTED,
        EventType.MODEL_RESPONSE_RECORDED,
        EventType.RUN_COMPLETED,
    ]
    assert provider.invocations_by_operation == {operation_id_for(created.run_id): 1}


async def test_request_is_built_from_the_run_and_budget(store: RunStore) -> None:
    provider = ScriptedModelProvider([REPLY])
    run_id = await _run(_runner(store, provider))
    assert provider.requests == [
        ModelRequest(
            operation_id=operation_id_for(run_id),
            model="fake",
            input="hello",
            instructions="be brief",
            max_output_tokens=64,
            timeout_s=30,
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


# --- policy defaults ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("deduplicates", "expected"),
    [(True, AmbiguousOutcomePolicy.RETRY), (False, AmbiguousOutcomePolicy.FAIL)],
)
async def test_default_ambiguous_policy_follows_provider_capabilities(
    deduplicates: bool, expected: AmbiguousOutcomePolicy
) -> None:
    caps = FAKE_DESCRIPTOR.model_copy(update={"deduplicates_requests": deduplicates})
    created = await _runner(InMemoryRunStore(), ScriptedModelProvider([], descriptor=caps)).create(
        SPEC
    )
    assert created.ambiguous_policy is expected


async def test_explicit_policy_overrides_the_default() -> None:
    provider = ScriptedModelProvider([], descriptor=NON_DEDUP)
    spec = SPEC.model_copy(update={"ambiguous_policy": AmbiguousOutcomePolicy.RETRY})
    created = await _runner(InMemoryRunStore(), provider).create(spec)
    assert created.ambiguous_policy is AmbiguousOutcomePolicy.RETRY


# --- failures, retries and budgets -------------------------------------------------------


@pytest.mark.parametrize("kind", [k for k in FailureKind if not k.retryable], ids=lambda k: k.value)
async def test_non_retryable_failure_fails_immediately(store: RunStore, kind: FailureKind) -> None:
    provider = ScriptedModelProvider([_error(kind, provider_request_id="req_x")])
    time = FakeTime()
    final = await load_run(store, await _run(_runner(store, provider, time)))

    assert final.status is RunStatus.FAILED
    assert final.failure is not None
    assert (final.failure.reason, final.failure.kind) == (FailureReason.PROVIDER_ERROR, kind)
    assert final.last_failure is not None
    assert final.last_failure.provider_request_id == "req_x"
    assert (provider.invocations, final.attempts, time.sleeps) == (1, 1, [])


async def test_transient_failure_is_retried_with_backoff(store: RunStore) -> None:
    provider = ScriptedModelProvider(
        [_error(FailureKind.OVERLOADED), _error(FailureKind.UNAVAILABLE), REPLY], per_attempt=True
    )
    time = FakeTime()
    final = await load_run(store, await _run(_runner(store, provider, time)))

    assert final.status is RunStatus.COMPLETED
    assert final.attempts == 3
    assert time.sleeps == [0.5, 1.0]  # exponential backoff, jitter pinned to its maximum
    assert _types(await store.read(final.run_id)) == [
        EventType.RUN_CREATED,
        EventType.CONTEXT_ASSEMBLED,
        EventType.MODEL_CALL_STARTED,
        EventType.MODEL_CALL_FAILED,
        EventType.MODEL_CALL_STARTED,
        EventType.MODEL_CALL_FAILED,
        EventType.MODEL_CALL_STARTED,
        EventType.MODEL_RESPONSE_RECORDED,
        EventType.RUN_COMPLETED,
    ]


async def test_retry_after_from_the_provider_is_honoured(store: RunStore) -> None:
    provider = ScriptedModelProvider(
        [_error(FailureKind.RATE_LIMITED, retry_after_s=7.5), REPLY], per_attempt=True
    )
    time = FakeTime()
    final = await load_run(store, await _run(_runner(store, provider, time)))
    assert final.status is RunStatus.COMPLETED
    assert time.sleeps == [7.5]


async def test_max_attempts_bounds_physical_attempts(store: RunStore) -> None:
    provider = ScriptedModelProvider([_error(FailureKind.OVERLOADED)] * 5, per_attempt=True)
    final = await load_run(store, await _run(_runner(store, provider)))

    assert final.status is RunStatus.FAILED
    assert final.failure is not None
    assert final.failure.reason is FailureReason.BUDGET_EXHAUSTED
    assert provider.invocations == final.attempts == BUDGET.max_attempts


async def test_retry_that_would_pass_the_deadline_fails_instead_of_sleeping(
    store: RunStore,
) -> None:
    budget = BUDGET.model_copy(update={"deadline_s": 5.0})
    provider = ScriptedModelProvider(
        [_error(FailureKind.RATE_LIMITED, retry_after_s=10), REPLY], per_attempt=True
    )
    time = FakeTime()
    final = await load_run(
        store,
        await _run(_runner(store, provider, time), SPEC.model_copy(update={"budget": budget})),
    )
    assert final.failure is not None
    assert final.failure.reason is FailureReason.BUDGET_EXHAUSTED
    assert (time.sleeps, provider.invocations) == ([], 1)


async def test_attempt_timeout_is_capped_by_the_remaining_deadline(store: RunStore) -> None:
    budget = BUDGET.model_copy(update={"deadline_s": 12.0})
    provider = ScriptedModelProvider([REPLY])
    time = FakeTime()
    runner = _runner(store, provider, time)
    run = await runner.create(SPEC.model_copy(update={"budget": budget}))
    time.now += timedelta(seconds=4)
    await runner.execute(run.run_id)
    assert provider.requests[0].timeout_s == pytest.approx(8.0)


async def test_run_resumed_after_its_deadline_fails_without_calling_the_provider(
    store: RunStore,
) -> None:
    budget = BUDGET.model_copy(update={"deadline_s": 60.0})
    provider = ScriptedModelProvider([REPLY])
    time = FakeTime()
    runner = _runner(store, provider, time)
    run = await runner.create(SPEC.model_copy(update={"budget": budget}))
    time.now += timedelta(minutes=5)

    final = await runner.execute(run.run_id)

    assert final.failure is not None
    assert final.failure.reason is FailureReason.BUDGET_EXHAUSTED
    assert provider.invocations == 0


async def test_runtime_enforces_the_attempt_timeout_on_a_hung_provider(store: RunStore) -> None:
    class Hung(ScriptedModelProvider):
        async def generate(self, request: ModelRequest) -> ModelResponse:
            await asyncio.sleep(60)
            raise AssertionError("unreachable")

    budget = BUDGET.model_copy(update={"attempt_timeout_s": 0.01, "max_attempts": 1})
    spec = SPEC.model_copy(update={"budget": budget})
    final = await load_run(store, await _run(_runner(store, Hung([], descriptor=NON_DEDUP)), spec))
    assert final.last_failure is not None
    assert final.last_failure.kind is FailureKind.TIMEOUT
    assert final.failure is not None
    assert final.failure.reason is FailureReason.AMBIGUOUS_OUTCOME


# --- ambiguous outcomes ------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind", [FailureKind.TIMEOUT, FailureKind.SERVER_ERROR, FailureKind.CONNECTION_LOST]
)
async def test_ambiguous_failure_is_not_retried_under_fail_policy(
    store: RunStore, kind: FailureKind
) -> None:
    provider = ScriptedModelProvider([_error(kind), REPLY], per_attempt=True, descriptor=NON_DEDUP)
    final = await load_run(store, await _run(_runner(store, provider)))

    assert final.ambiguous_policy is AmbiguousOutcomePolicy.FAIL
    assert final.failure is not None
    assert (final.failure.reason, final.failure.kind) == (FailureReason.AMBIGUOUS_OUTCOME, kind)
    assert provider.invocations == 1


async def test_ambiguous_failure_is_retried_under_retry_policy(store: RunStore) -> None:
    provider = ScriptedModelProvider(
        [_error(FailureKind.TIMEOUT), REPLY], per_attempt=True, descriptor=NON_DEDUP
    )
    spec = SPEC.model_copy(update={"ambiguous_policy": AmbiguousOutcomePolicy.RETRY})
    final = await load_run(store, await _run(_runner(store, provider), spec))
    assert final.status is RunStatus.COMPLETED
    assert provider.invocations == 2


async def test_unexpected_exception_leaves_run_in_flight_then_resume_applies_policy(
    store: RunStore,
) -> None:
    class Broken(ScriptedModelProvider):
        async def generate(self, request: ModelRequest) -> ModelResponse:
            raise RuntimeError("adapter bug")

    run = await _runner(store, Broken([], descriptor=NON_DEDUP)).create(SPEC)
    with pytest.raises(RuntimeError):
        await _runner(store, Broken([], descriptor=NON_DEDUP)).execute(run.run_id)
    assert (await load_run(store, run.run_id)).status is RunStatus.MODEL_CALL_IN_FLIGHT

    fresh = ScriptedModelProvider([REPLY], descriptor=NON_DEDUP)
    final = await _runner(store, fresh).execute(run.run_id)

    log = await store.read(run.run_id)
    recorded = [e.payload for e in log if isinstance(e.payload, ModelCallFailed)]
    assert [f.failure.kind for f in recorded] == [FailureKind.INTERRUPTED]
    assert final.failure is not None
    assert final.failure.reason is FailureReason.AMBIGUOUS_OUTCOME
    assert fresh.invocations == 0


# --- idempotency of execute, provider identity, concurrency --------------------------------


@pytest.mark.parametrize(
    "script",
    [[REPLY], [_error(FailureKind.INVALID_REQUEST)]],
    ids=["completed", "failed"],
)
async def test_execute_on_terminal_run_is_a_noop(
    store: RunStore, script: list[ModelResponse | ModelCallError]
) -> None:
    run_id = await _run(_runner(store, ScriptedModelProvider(script)))
    final, log = await load_run(store, run_id), await store.read(run_id)

    untouched = ScriptedModelProvider([])
    assert await _runner(store, untouched).execute(run_id) == final
    assert await store.read(run_id) == log
    assert untouched.invocations == 0


async def test_resuming_after_the_deployment_changed_is_refused(store: RunStore) -> None:
    run = await _runner(store, ScriptedModelProvider([])).create(SPEC)
    other = ScriptedModelProvider(
        [REPLY], descriptor=FAKE_DESCRIPTOR.model_copy(update={"endpoint": "http://other:8000"})
    )
    with pytest.raises(DeploymentChanged):
        await _runner(store, other).execute(run.run_id)
    assert other.invocations == 0


async def test_runs_are_independent(store: RunStore) -> None:
    runner = _runner(
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


# --- deployment resolution and capability validation -----------------------------------


async def test_unknown_model_is_rejected_before_persisting() -> None:
    store = InMemoryRunStore()
    with pytest.raises(UnknownModel):
        await _runner(store, ScriptedModelProvider([])).create(
            SPEC.model_copy(update={"model": "not-configured"})
        )


@pytest.mark.parametrize(
    ("text", "spec_update", "message"),
    [
        (TextCapabilities(system_prompt=False), {}, "system prompt"),
        (TextCapabilities(max_output_tokens=32), {}, "exceeds the limit 32"),
        (None, {}, "text generation is not supported"),
    ],
)
async def test_unsupported_capabilities_are_rejected_before_persisting(
    text: TextCapabilities | None, spec_update: dict[str, object], message: str
) -> None:
    descriptor = FAKE_DESCRIPTOR.model_copy(update={"capabilities": ModelCapabilities(text=text)})
    runner = _runner(InMemoryRunStore(), ScriptedModelProvider([], descriptor=descriptor))
    with pytest.raises(CapabilityMismatch, match=message):
        await runner.create(SPEC.model_copy(update=spec_update))


async def test_deployment_change_names_the_changed_fields(store: RunStore) -> None:
    run = await _runner(store, ScriptedModelProvider([])).create(SPEC)
    moved = FAKE_DESCRIPTOR.model_copy(update={"model": "fake-v2", "locality": Locality.EXTERNAL})
    with pytest.raises(DeploymentChanged, match=r"\['locality', 'model'\]"):
        await _runner(store, ScriptedModelProvider([], descriptor=moved)).execute(run.run_id)


async def test_requests_carry_the_deployments_wire_model(store: RunStore) -> None:
    descriptor = FAKE_DESCRIPTOR.model_copy(update={"model": "org/model-7b-instruct"})
    provider = ScriptedModelProvider([REPLY], descriptor=descriptor)
    await _run(_runner(store, provider))
    assert provider.requests[0].model == "org/model-7b-instruct"
