"""Recovery from a crash at every boundary of a single-turn run (M1 plan §7).

Counting vocabulary:
- intents: ModelCallStarted events (attempts initiated; an intent may never reach the provider)
- invocations: physical calls the provider received
- generations: successful logical generations (responses produced, deduplicated by operation
  id); definitive rejections are counted separately as provider.rejections
- recorded: ModelResponseRecorded events (durable responses; at most one per logical call)
"""

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import pytest

from hollis.adapters.fake_provider import ScriptedModelProvider
from hollis.adapters.sqlite_store import SqliteRunStore
from hollis.runtime.events import EventEnvelope, EventType, ModelResponseRecorded
from hollis.runtime.model import ModelProvider, ModelResponse
from hollis.runtime.runner import Runner, RunSpec
from hollis.runtime.state import RunState, RunStatus, fold
from hollis.runtime.store import RunStore, load_run
from tests.support.crash import CrashAfterGenerate, CrashingStore, SimulatedCrash
from tests.support.crash_child import CHILD_RESPONSE

SPEC = RunSpec(model="fake", input="hello", instructions="be brief")
FIRST = ModelResponse(content="first generation")
SECOND = ModelResponse(content="a second generation must never be recorded")


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
}

CRASH_AFTER_COMMIT: dict[str, EventType] = {
    "1-after-run-created": EventType.RUN_CREATED,
    "2-after-model-call-started": EventType.MODEL_CALL_STARTED,
    "4-after-response-recorded": EventType.MODEL_RESPONSE_RECORDED,
    "5-after-run-completed": EventType.RUN_COMPLETED,
}


def _count(log: list[EventEnvelope], event_type: EventType) -> int:
    return sum(e.event_type is event_type for e in log)


async def _crash(store: RunStore, provider: ScriptedModelProvider, boundary: str) -> UUID:
    """Run until the injected crash and return the run id."""
    crashing_store = CrashingStore(store, CRASH_AFTER_COMMIT.get(boundary))
    crashing_provider: ModelProvider = (
        CrashAfterGenerate(provider) if boundary.startswith("3-") else provider
    )
    runner = Runner(crashing_store, crashing_provider)

    async def create_and_execute() -> None:
        run = await runner.create(SPEC)
        await runner.execute(run.run_id)

    with pytest.raises(SimulatedCrash):
        await create_and_execute()
    assert crashing_store.run_id is not None
    return crashing_store.run_id


async def _uninterrupted(store: RunStore) -> RunState:
    runner = Runner(store, ScriptedModelProvider([FIRST]))
    run = await runner.create(SPEC)
    return await runner.execute(run.run_id)


@pytest.mark.parametrize("boundary", BOUNDARIES)
async def test_recovery_from_boundary(store: RunStore, boundary: str) -> None:
    expected = BOUNDARIES[boundary]
    provider = ScriptedModelProvider([FIRST, SECOND])  # survives the crash, like a real provider

    run_id = await _crash(store, provider, boundary)
    assert provider.invocations == expected.invocations_before
    log_before = await store.read(run_id)

    final = await Runner(store, provider).execute(run_id)

    log = await store.read(run_id)
    assert final.status is RunStatus.COMPLETED
    assert final == fold(log) == await load_run(store, run_id)
    assert provider.invocations == expected.invocations_after
    assert provider.generations == 1  # the logical generation was never repeated
    assert provider.rejections == 0
    assert _count(log, EventType.MODEL_CALL_STARTED) == expected.intents
    assert _count(log, EventType.MODEL_RESPONSE_RECORDED) == 1
    assert final.response == FIRST
    if expected.log_unchanged_by_resume:
        assert log == log_before

    baseline = await _uninterrupted(store)
    for field in ("status", "response", "model", "input", "instructions", "failure"):
        assert getattr(final, field) == getattr(baseline, field), field


async def test_ambiguous_outcome_is_retried_under_the_same_operation_id(store: RunStore) -> None:
    provider = ScriptedModelProvider([FIRST, SECOND])
    run_id = await _crash(store, provider, "3-after-generate-before-record")

    await Runner(store, provider).execute(run_id)

    log = await store.read(run_id)
    (operation_id,) = provider.invocations_by_operation
    assert provider.invocations_by_operation[operation_id] == 2
    recorded = [e.payload for e in log if isinstance(e.payload, ModelResponseRecorded)]
    assert [(r.operation_id, r.attempt, r.response) for r in recorded] == [(operation_id, 2, FIRST)]


@pytest.mark.parametrize("boundary", ["4-after-response-recorded", "5-after-run-completed"])
async def test_recorded_response_never_reinvokes_the_provider(
    store: RunStore, boundary: str
) -> None:
    run_id = await _crash(store, ScriptedModelProvider([FIRST]), boundary)

    fresh = ScriptedModelProvider([])  # any invocation would raise ScriptExhausted
    final = await Runner(store, fresh).execute(run_id)

    assert final.status is RunStatus.COMPLETED
    assert final.response == FIRST
    assert fresh.invocations == 0


async def test_repeated_crashes_before_invocation_only_add_intents(store: RunStore) -> None:
    provider = ScriptedModelProvider([FIRST])
    run_id = await _crash(store, provider, "2-after-model-call-started")
    for _ in range(2):
        with pytest.raises(SimulatedCrash):
            await Runner(CrashingStore(store, EventType.MODEL_CALL_STARTED), provider).execute(
                run_id
            )

    final = await Runner(store, provider).execute(run_id)

    assert final.attempts == 4
    assert provider.invocations == 1
    assert _count(await store.read(run_id), EventType.MODEL_CALL_STARTED) == 4


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
    final = await Runner(store, fresh).execute(run_id)

    assert final.status is RunStatus.COMPLETED
    assert final.response == CHILD_RESPONSE
    assert fresh.invocations == 0
