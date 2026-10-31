"""The runtime driving the Anthropic adapter end to end, over a mock transport (no network)."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from hollis.adapters.sqlite_store import SqliteRunStore
from hollis.runtime.events import EventType, FailureReason, serialize_event
from hollis.runtime.model import FailureKind, StopReason
from hollis.runtime.policy import AmbiguousOutcomePolicy, RunBudget
from hollis.runtime.redaction import REDACTED, Redactor
from hollis.runtime.runner import Runner, RunSpec
from hollis.runtime.state import RunState, RunStatus
from hollis.runtime.store import RunStore
from tests.support.anthropic import TEST_KEY, MockAnthropic, api_error, ok
from tests.support.events import T0

SPEC = RunSpec(
    model="claude-opus-5-5",
    input="Say hello.",
    budget=RunBudget(max_output_tokens=512, max_attempts=3, attempt_timeout_s=60),
)


class _Time:
    def __init__(self) -> None:
        self.now = T0
        self.sleeps: list[float] = []

    def clock(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


async def _execute(
    store: RunStore, mock: MockAnthropic, spec: RunSpec = SPEC, time: _Time | None = None
) -> RunState:
    time = time or _Time()
    runner = Runner(
        store,
        mock.provider(),
        clock=time.clock,
        sleep=time.sleep,
        jitter=lambda: 1.0,
        redactor=Redactor([TEST_KEY]),
    )
    run = await runner.create(spec)
    return await runner.execute(run.run_id)


async def test_successful_run_records_provider_metadata(store: RunStore) -> None:
    mock = MockAnthropic(ok(request_id="req_live_like_1"))
    final = await _execute(store, mock)

    assert final.status is RunStatus.COMPLETED
    assert final.deployment.provider == "anthropic"
    assert final.ambiguous_policy is AmbiguousOutcomePolicy.FAIL  # no provider idempotency
    assert final.response is not None
    assert final.response.provider_request_id == "req_live_like_1"
    assert final.response.stop_reason is StopReason.END_TURN
    assert final.usage.model_dump() == {"input_tokens": 25, "output_tokens": 40}
    assert mock.calls == 1


async def test_overload_is_retried_honouring_retry_after(store: RunStore) -> None:
    mock = MockAnthropic(
        api_error(429, "rate_limit_error", headers={"retry-after": "3"}),
        api_error(529, "overloaded_error"),
        ok(),
    )
    time = _Time()
    final = await _execute(store, mock, time=time)

    assert final.status is RunStatus.COMPLETED
    assert final.attempts == mock.calls == 3
    assert time.sleeps == [3.0, 1.0]


async def test_server_error_is_ambiguous_and_not_retried_by_default(store: RunStore) -> None:
    mock = MockAnthropic(api_error(500, "api_error"), ok())
    final = await _execute(store, mock)

    assert final.failure is not None
    assert (final.failure.reason, final.failure.kind) == (
        FailureReason.AMBIGUOUS_OUTCOME,
        FailureKind.SERVER_ERROR,
    )
    assert mock.calls == 1


async def test_explicit_retry_policy_accepts_the_risk_of_a_second_generation(
    store: RunStore,
) -> None:
    mock = MockAnthropic(api_error(500, "api_error"), ok())
    spec = SPEC.model_copy(update={"ambiguous_policy": AmbiguousOutcomePolicy.RETRY})
    final = await _execute(store, mock, spec)
    assert final.status is RunStatus.COMPLETED
    assert mock.calls == 2


async def test_authentication_failure_is_terminal(store: RunStore) -> None:
    mock = MockAnthropic(api_error(401, "authentication_error", "invalid x-api-key"))
    final = await _execute(store, mock)
    assert final.failure is not None
    assert (final.failure.reason, final.failure.kind) == (
        FailureReason.PROVIDER_ERROR,
        FailureKind.AUTHENTICATION,
    )
    assert mock.calls == 1


async def test_key_echoed_by_the_api_never_reaches_storage(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "runs.db"
    store = await SqliteRunStore.open(db)
    mock = MockAnthropic(api_error(400, "invalid_request_error", f"bad header {TEST_KEY}"))

    final = await _execute(store, mock)

    events = await store.read(final.run_id)
    assert [e.event_type for e in events][-1] is EventType.RUN_FAILED
    serialized = "".join(serialize_event(e) for e in events)
    assert TEST_KEY not in serialized
    assert REDACTED in serialized
    assert TEST_KEY.encode() not in db.read_bytes()
    assert all(TEST_KEY not in r.getMessage() for r in caplog.records)
