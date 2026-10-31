"""Crash recovery with the Anthropic adapter, which cannot deduplicate requests.

The crash lands after Anthropic generated (and billed) a response but before Hollis recorded it.
Only the explicit `retry` policy sends a second request; the default fails the run instead.
"""

import pytest

from hollis.runtime.events import EventType, FailureReason
from hollis.runtime.model import FailureKind
from hollis.runtime.policy import AmbiguousOutcomePolicy, RunBudget
from hollis.runtime.runner import Runner, RunSpec
from hollis.runtime.state import RunStatus
from hollis.runtime.store import RunStore
from tests.support.anthropic import MockAnthropic, ok
from tests.support.crash import CrashAfterGenerate, CrashingStore, SimulatedCrash

SPEC = RunSpec(model="claude", input="hi", budget=RunBudget(max_output_tokens=64))


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.parametrize(
    ("policy", "status", "http_calls"),
    [
        (AmbiguousOutcomePolicy.FAIL, RunStatus.FAILED, 1),
        (AmbiguousOutcomePolicy.RETRY, RunStatus.COMPLETED, 2),
    ],
)
async def test_crash_after_generation_respects_the_ambiguous_policy(
    store: RunStore, policy: AmbiguousOutcomePolicy, status: RunStatus, http_calls: int
) -> None:
    mock = MockAnthropic(ok(), ok())
    provider = mock.provider()
    spec = SPEC.model_copy(update={"ambiguous_policy": policy})
    crashing = Runner(CrashingStore(store), CrashAfterGenerate(provider), sleep=_no_sleep)
    run = await crashing.create(spec)
    with pytest.raises(SimulatedCrash):
        await crashing.execute(run.run_id)
    assert mock.calls == 1

    final = await Runner(store, provider, sleep=_no_sleep).execute(run.run_id)

    assert final.status is status
    assert mock.calls == http_calls
    if status is RunStatus.FAILED:
        assert final.failure is not None
        assert (final.failure.reason, final.failure.kind) == (
            FailureReason.AMBIGUOUS_OUTCOME,
            FailureKind.INTERRUPTED,
        )


async def test_recorded_response_never_triggers_another_request(store: RunStore) -> None:
    mock = MockAnthropic(ok())
    provider = mock.provider()
    crashing = Runner(CrashingStore(store, EventType.MODEL_RESPONSE_RECORDED), provider)
    run = await crashing.create(SPEC)
    with pytest.raises(SimulatedCrash):
        await crashing.execute(run.run_id)

    final = await Runner(store, provider).execute(run.run_id)

    assert final.status is RunStatus.COMPLETED
    assert mock.calls == 1  # the mock would fail the test on any further request
