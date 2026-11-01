"""Run lifecycle through the platform: background execution, cancellation, resumption, recovery
after a restart, the run index, and live observation."""

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from hollis.platform.auth import Principal
from hollis.platform.errors import Conflict, InvalidRequest, NotFound
from hollis.platform.schemas import CancelInput, EventView, RunPhase, StartRun
from hollis.platform.services import Platform
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import RunSpec
from hollis.runtime.state import RunState
from tests.support.platform import add_agent, run_to_rest


async def test_starting_a_run_returns_before_it_finishes(
    platform: Platform, admin: Principal
) -> None:
    await add_agent(platform, admin)
    started = await platform.start_run(admin, StartRun(input="hello", agent="helper"))
    assert started.phase in (RunPhase.QUEUED, RunPhase.RUNNING)
    done = await platform.wait_run(admin, started.run_id, timeout_s=10)
    assert done.phase is RunPhase.COMPLETED
    assert done.turns == 1


async def test_cancelling_a_run_waiting_for_approval(platform: Platform, admin: Principal) -> None:
    await add_agent(platform, admin)
    waiting = await run_to_rest(platform, admin, "write a.txt: x")

    cancelled = await platform.cancel_run(
        admin, waiting.run_id, CancelInput(reason="changed my mind")
    )

    assert cancelled.phase is RunPhase.CANCELLED
    detail = await platform.run(admin, waiting.run_id)
    assert detail.cancellation is not None
    assert (detail.cancellation.by, detail.cancellation.reason) == ("admin", "changed my mind")
    assert await platform.approvals(admin) == []
    with pytest.raises(Conflict):
        await platform.resume_run(admin, waiting.run_id)
    assert not (platform.ws.root / "a.txt").exists()


async def test_unknown_runs_agents_and_models_are_reported(
    platform: Platform, admin: Principal
) -> None:
    with pytest.raises(NotFound):
        await platform.run(admin, uuid4())
    with pytest.raises(NotFound):
        await platform.start_run(admin, StartRun(input="x", agent="nobody"))
    with pytest.raises(InvalidRequest):
        await platform.start_run(admin, StartRun(input="x", model="no-such-model"))
    with pytest.raises(InvalidRequest):
        await platform.start_run(admin, StartRun(input="x", model="demo", tools=("no_such_tool",)))
    with pytest.raises(NotFound):  # the configured default agent does not exist yet
        await platform.start_run(admin, StartRun(input="x"))
    assert (await platform.runs(admin)).total == 0, "nothing is persisted for rejected runs"


async def test_runs_without_an_agent_use_the_default_agent(
    platform: Platform, admin: Principal
) -> None:
    starter = await platform.create_starter(admin)
    assert starter.name == platform.ws.config.runtime.default_agent == "assistant"
    assert await platform.create_starter(admin) == starter, "idempotent"
    done = await platform.wait_run(
        admin, (await platform.start_run(admin, StartRun(input="list files"))).run_id, timeout_s=10
    )
    assert done.agent == "assistant"
    assert done.phase is RunPhase.COMPLETED
    assert [c.name for c in await platform.collections(admin)] == ["docs"]


async def test_a_run_interrupted_by_a_restart_is_recovered(
    tmp_path: Path, platform: Platform
) -> None:
    admin = await platform.local_admin()
    # Persist a run without executing it: what a process killed right after creating it leaves.
    state = await platform.ws.runner.create(
        RunSpec(
            model="demo",
            input="list files",
            budget=RunBudget(max_output_tokens=256),
            tools=("fs_list_dir",),
        )
    )
    await platform.close()

    reopened = await Platform.open(tmp_path)
    try:
        listed = await reopened.run_summary(admin, state.run_id)
        assert listed.phase is RunPhase.QUEUED
        assert await reopened.ws.recover() == [state.run_id]
        done = await reopened.wait_run(admin, state.run_id, timeout_s=10)
        assert done.phase is RunPhase.COMPLETED
        assert await reopened.ws.recover() == [], "finished runs are not resumed again"
    finally:
        await reopened.close()


async def test_runs_waiting_for_approval_are_not_resumed_by_recovery(
    platform: Platform, admin: Principal
) -> None:
    await add_agent(platform, admin)
    waiting = await run_to_rest(platform, admin, "write a.txt: x")
    assert await platform.ws.recover() == []
    assert (await platform.run_summary(admin, waiting.run_id)).phase is RunPhase.WAITING_APPROVAL


async def test_the_run_index_is_re_derived_from_the_event_logs(
    platform: Platform, admin: Principal
) -> None:
    await add_agent(platform, admin)
    done = await run_to_rest(platform, admin, "list files")
    # Simulate a lost index update: the row lags behind the log.
    await platform.ws.db.execute(
        "UPDATE run_index SET status = 'pending', version = 1, data = '{}' WHERE run_id = ?",
        (str(done.run_id),),
    )
    assert (await platform.run_summary(admin, done.run_id)).phase is not RunPhase.COMPLETED

    assert await platform.ws.sync_index() == 1
    restored = await platform.run_summary(admin, done.run_id)
    assert restored.phase is RunPhase.COMPLETED
    assert restored.tool_calls == 1
    assert restored.created_by == "admin", "ownership is kept when derived columns are rebuilt"
    assert await platform.ws.sync_index() == 0


async def test_an_execution_error_leaves_the_run_resumable(
    platform: Platform, admin: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_agent(platform, admin)
    runner = platform.ws.runner
    original = runner.execute

    async def failing(run_id: UUID) -> RunState:
        raise RuntimeError(f"disk on fire while running {run_id}")

    monkeypatch.setattr(runner, "execute", failing)
    started = await platform.start_run(admin, StartRun(input="hello", agent="helper"))
    stopped = await platform.wait_run(admin, started.run_id, timeout_s=10)
    assert stopped.phase is RunPhase.INTERRUPTED
    assert stopped.error is not None
    assert "disk on fire" in stopped.error
    detail = await platform.run(admin, started.run_id)
    assert detail.summary.error == stopped.error

    monkeypatch.setattr(runner, "execute", original)
    await platform.resume_run(admin, started.run_id)
    done = await platform.wait_run(admin, started.run_id, timeout_s=10)
    assert done.phase is RunPhase.COMPLETED
    assert done.error is None


async def test_watching_a_run_streams_its_events_until_it_finishes(
    platform: Platform, admin: Principal
) -> None:
    await add_agent(platform, admin)
    started = await platform.start_run(admin, StartRun(input="list files", agent="helper"))
    seen: list[EventView] = []

    async def watch() -> None:
        async for event in platform.watch(admin, started.run_id, heartbeat=0.05):
            if event is not None:
                seen.append(event)

    await asyncio.wait_for(watch(), timeout=10)
    assert [e.sequence for e in seen] == list(range(1, len(seen) + 1))
    assert seen[-1].type == "run_completed"

    later = [e async for e in platform.watch(admin, started.run_id, after=len(seen) - 2)]
    assert [e.sequence for e in later if e is not None] == [len(seen) - 1, len(seen)]


async def test_watching_a_run_waiting_for_approval_sends_heartbeats(
    platform: Platform, admin: Principal
) -> None:
    await add_agent(platform, admin)
    waiting = await run_to_rest(platform, admin, "write a.txt: x")
    stream = platform.watch(admin, waiting.run_id, after=waiting.turns and 10_000, heartbeat=0.01)
    assert await anext(stream) is None
    await stream.aclose()


async def test_a_run_stopped_by_shutdown_mid_model_call_resumes_after_restart(
    tmp_path: Path, platform: Platform, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hollis.adapters.demo_model import DemoModelProvider

    admin = await platform.local_admin()
    await platform.create_starter(admin)
    entered = asyncio.Event()
    original = DemoModelProvider.generate

    async def hangs_once(self: DemoModelProvider, request: Any) -> Any:
        if not entered.is_set():
            entered.set()
            await asyncio.sleep(3600)  # the process stops while this call is in flight
        return await original(self, request)

    monkeypatch.setattr(DemoModelProvider, "generate", hangs_once)
    started = await platform.start_run(admin, StartRun(input="list files"))
    await asyncio.wait_for(entered.wait(), timeout=10)
    await platform.close()  # shutdown: the supervisor cancels the task; the log keeps the intent

    reopened = await Platform.open(tmp_path)
    try:
        assert await reopened.ws.recover() == [started.run_id]
        done = await reopened.wait_run(admin, started.run_id, timeout_s=10)
        assert done.phase is RunPhase.COMPLETED
        detail = await reopened.run(admin, started.run_id)
        failed = [a for a in detail.turns[0].model_call.attempts if a.outcome == "failed"]
        assert [a.failure_kind for a in failed] == ["interrupted"], "the interruption is recorded"
    finally:
        await reopened.close()
