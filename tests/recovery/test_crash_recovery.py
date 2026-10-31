"""Recovery from a crash at every boundary of a single-turn run.

Counting vocabulary:
- intents: ModelCallStarted events (attempts initiated; an intent may never reach the provider)
- invocations: physical calls the provider received
- generations: successful logical generations (responses produced, deduplicated by operation
  id); scripted failures are counted separately as provider.rejections
- recorded: ModelResponseRecorded events (durable responses; at most one per logical call)

The deduplicating fake defaults to the `retry` ambiguous-outcome policy, so interrupted attempts
are retried. The non-deduplicating matrix shows the `fail` default instead (ADR-0007).
"""

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest

from hollis.adapters.fake_provider import FAKE_DESCRIPTOR, ScriptedModelProvider
from hollis.adapters.sqlite_store import SqliteRunStore
from hollis.runtime.events import (
    EventEnvelope,
    EventType,
    FailureReason,
    ModelCallFailed,
    ModelResponseRecorded,
)
from hollis.runtime.model import (
    DeploymentDescriptor,
    FailureKind,
    ModelCallError,
    ModelProvider,
    ModelResponse,
)
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import Runner, RunSpec
from hollis.runtime.state import RunState, RunStatus, fold
from hollis.runtime.store import RunStore, load_run
from tests.support.crash import CrashAfterGenerate, CrashingStore, SimulatedCrash
from tests.support.crash_child import CHILD_RESPONSE

SPEC = RunSpec(
    model="fake",
    input="hello",
    instructions="be brief",
    budget=RunBudget(max_output_tokens=64, max_attempts=5),
)
FIRST = ModelResponse(content="first generation")
SECOND = ModelResponse(content="a second generation must never be recorded")
OVERLOADED = ModelCallError(FailureKind.OVERLOADED, "overloaded")
NON_DEDUP = FAKE_DESCRIPTOR.model_copy(update={"deduplicates_requests": False})


async def _no_sleep(_: float) -> None:
    return None


def _runner(store: RunStore, provider: ModelProvider) -> Runner:
    return Runner(store, provider, sleep=_no_sleep)


@dataclass(frozen=True)
class Expected:
    invocations_before: int
    invocations_after: int
    intents: int
    log_unchanged_by_resume: bool = False


BOUNDARIES: dict[str, Expected] = {
    "1-after-run-created": Expected(invocations_before=0, invocations_after=1, intents=1),
    "2-after-model-call-started": Expected(invocations_before=0, invocations_after=1, intents=2),
    "3-after-generate-before-record": Expected(
        invocations_before=1, invocations_after=2, intents=2
    ),
    "4-after-response-recorded": Expected(invocations_before=1, invocations_after=1, intents=1),
    "5-after-run-completed": Expected(
        invocations_before=1, invocations_after=1, intents=1, log_unchanged_by_resume=True
    ),
    "6-after-model-call-failed": Expected(invocations_before=1, invocations_after=2, intents=2),
}

CRASH_AFTER_COMMIT: dict[str, EventType] = {
    "1-after-run-created": EventType.RUN_CREATED,
    "2-after-model-call-started": EventType.MODEL_CALL_STARTED,
    "4-after-response-recorded": EventType.MODEL_RESPONSE_RECORDED,
    "5-after-run-completed": EventType.RUN_COMPLETED,
    "6-after-model-call-failed": EventType.MODEL_CALL_FAILED,
}


def _script(boundary: str) -> list[ModelResponse | ModelCallError]:
    # Boundary 6 needs a recorded (transient) failure before the crash.
    return [OVERLOADED, FIRST, SECOND] if boundary.startswith("6-") else [FIRST, SECOND]


def _provider(
    boundary: str, descriptor: DeploymentDescriptor = FAKE_DESCRIPTOR
) -> ScriptedModelProvider:
    return ScriptedModelProvider(_script(boundary), per_attempt=True, descriptor=descriptor)


def _count(log: list[EventEnvelope], event_type: EventType) -> int:
    return sum(e.event_type is event_type for e in log)


async def _crash(store: RunStore, provider: ScriptedModelProvider, boundary: str) -> UUID:
    """Run until the injected crash and return the run id."""
    crashing_store = CrashingStore(store, CRASH_AFTER_COMMIT.get(boundary))
    crashing_provider: ModelProvider = (
        CrashAfterGenerate(provider) if boundary.startswith("3-") else provider
    )
    runner = _runner(crashing_store, crashing_provider)

    async def create_and_execute() -> None:
        run = await runner.create(SPEC)
        await runner.execute(run.run_id)

    with pytest.raises(SimulatedCrash):
        await create_and_execute()
    assert crashing_store.run_id is not None
    return crashing_store.run_id


async def _uninterrupted(store: RunStore) -> RunState:
    runner = _runner(store, ScriptedModelProvider([FIRST]))
    run = await runner.create(SPEC)
    return await runner.execute(run.run_id)


@pytest.mark.parametrize("boundary", BOUNDARIES)
async def test_recovery_from_boundary(store: RunStore, boundary: str) -> None:
    expected = BOUNDARIES[boundary]
    provider = _provider(boundary)  # survives the crash, like a real provider

    run_id = await _crash(store, provider, boundary)
    assert provider.invocations == expected.invocations_before
    log_before = await store.read(run_id)

    final = await _runner(store, provider).execute(run_id)

    log = await store.read(run_id)
    assert final.status is RunStatus.COMPLETED
    assert final == fold(log) == await load_run(store, run_id)
    assert provider.invocations == expected.invocations_after
    assert provider.generations == 1  # the logical generation was never repeated
    assert _count(log, EventType.MODEL_CALL_STARTED) == expected.intents
    assert _count(log, EventType.MODEL_RESPONSE_RECORDED) == 1
    # Every attempt before the successful one has exactly one recorded failure.
    assert _count(log, EventType.MODEL_CALL_FAILED) == expected.intents - 1
    assert final.response == FIRST
    if expected.log_unchanged_by_resume:
        assert log == log_before

    baseline = await _uninterrupted(store)
    for field in ("status", "response", "model", "input", "instructions", "failure"):
        assert getattr(final, field) == getattr(baseline, field), field


async def test_interrupted_attempt_is_recorded_then_retried_under_the_same_operation_id(
    store: RunStore,
) -> None:
    provider = _provider("3-after-generate-before-record")
    run_id = await _crash(store, provider, "3-after-generate-before-record")

    await _runner(store, provider).execute(run_id)

    log = await store.read(run_id)
    (operation_id,) = provider.invocations_by_operation
    assert provider.invocations_by_operation[operation_id] == 2
    failed = [e.payload for e in log if isinstance(e.payload, ModelCallFailed)]
    assert [(f.attempt, f.failure.kind) for f in failed] == [(1, FailureKind.INTERRUPTED)]
    recorded = [e.payload for e in log if isinstance(e.payload, ModelResponseRecorded)]
    assert [(r.operation_id, r.attempt, r.response) for r in recorded] == [(operation_id, 2, FIRST)]


@pytest.mark.parametrize("boundary", ["4-after-response-recorded", "5-after-run-completed"])
async def test_recorded_response_never_reinvokes_the_provider(
    store: RunStore, boundary: str
) -> None:
    run_id = await _crash(store, ScriptedModelProvider([FIRST]), boundary)

    fresh = ScriptedModelProvider([])  # any invocation would raise ScriptExhausted
    final = await _runner(store, fresh).execute(run_id)

    assert final.status is RunStatus.COMPLETED
    assert final.response == FIRST
    assert fresh.invocations == 0


# --- a provider that does not deduplicate: the default policy is `fail` ---------------------


@pytest.mark.parametrize(
    ("boundary", "outcome", "invocations_after"),
    [
        ("1-after-run-created", RunStatus.COMPLETED, 1),
        ("2-after-model-call-started", RunStatus.FAILED, 0),  # outcome unknown to the runtime
        ("3-after-generate-before-record", RunStatus.FAILED, 1),  # generated, then lost
        ("4-after-response-recorded", RunStatus.COMPLETED, 1),
        ("6-after-model-call-failed", RunStatus.COMPLETED, 2),  # not processed: safe to retry
    ],
)
async def test_non_deduplicating_provider_never_risks_a_second_generation(
    store: RunStore, boundary: str, outcome: RunStatus, invocations_after: int
) -> None:
    provider = _provider(boundary, NON_DEDUP)
    run_id = await _crash(store, provider, boundary)

    final = await _runner(store, provider).execute(run_id)

    assert final.status is outcome
    assert provider.invocations == invocations_after
    assert provider.generations <= 1
    if outcome is RunStatus.FAILED:
        assert final.failure is not None
        assert (final.failure.reason, final.failure.kind) == (
            FailureReason.AMBIGUOUS_OUTCOME,
            FailureKind.INTERRUPTED,
        )


async def test_repeated_crashes_before_invocation_only_add_intents(store: RunStore) -> None:
    provider = ScriptedModelProvider([FIRST])
    run_id = await _crash(store, provider, "2-after-model-call-started")
    for _ in range(2):
        with pytest.raises(SimulatedCrash):
            await _runner(CrashingStore(store, EventType.MODEL_CALL_STARTED), provider).execute(
                run_id
            )

    final = await _runner(store, provider).execute(run_id)

    assert final.attempts == 4
    assert provider.invocations == 1
    log = await store.read(run_id)
    assert _count(log, EventType.MODEL_CALL_STARTED) == 4
    assert _count(log, EventType.MODEL_CALL_FAILED) == 3


@pytest.mark.skipif(sys.platform == "win32", reason="relies on POSIX SIGKILL")
async def test_recovery_after_process_is_killed(tmp_path: Path) -> None:
    """Process death after ModelResponseRecorded commits. Exercises process-level recovery
    only; it does not simulate OS crashes or power loss (no fsync/torn-write testing)."""
    db = tmp_path / "runs.db"
    child = Path(__file__).parents[1] / "support" / "crash_child.py"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(child), str(db), stdout=asyncio.subprocess.PIPE
    )
    assert proc.stdout is not None
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
    finally:
        proc.kill()
        await proc.wait()
    marker, run_id_text = line.decode().split()
    assert marker == "RECORDED"
    assert proc.returncode == -9

    store = await SqliteRunStore.open(db)
    run_id = UUID(run_id_text)
    assert (await load_run(store, run_id)).status is RunStatus.MODEL_RESPONDED

    fresh = ScriptedModelProvider([])
    final = await _runner(store, fresh).execute(run_id)

    assert final.status is RunStatus.COMPLETED
    assert final.response == CHILD_RESPONSE
    assert fresh.invocations == 0
