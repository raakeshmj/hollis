"""Cancellation, run observation and run listing (M6 runtime additions)."""

import asyncio
from typing import Any
from uuid import uuid4

import pytest

from hollis.adapters.fake_provider import ScriptedModelProvider
from hollis.runtime.events import EventEnvelope, EventType, RunCancelled
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import ApprovalMismatch, Runner, RunSpec
from hollis.runtime.state import InvalidEventSequence, RunState, RunStatus, fold
from hollis.runtime.store import RunStore
from hollis.runtime.tools import ApprovalDecision, ToolEffect, ToolOutput
from tests.support.events import build_log, created
from tests.support.tools import Catalog, FakeTool, answer, call, wants

BUDGET = RunBudget(max_output_tokens=32)


class Deferring:
    async def review(self, request: Any) -> None:
        return None


async def test_cancelling_a_run_waiting_for_approval(store: RunStore) -> None:
    tool = FakeTool("deploy", effect=ToolEffect.MUTATING)
    provider = ScriptedModelProvider([wants(call("deploy", "c1"))])
    runner = Runner(store, provider, tools=Catalog(tool), approver=Deferring())
    run = await runner.create(
        RunSpec(model="fake", input="ship it", tools=("deploy",), budget=BUDGET)
    )
    waiting = await runner.execute(run.run_id)
    assert waiting.awaiting_approval
    [request] = await runner.pending_approvals(run.run_id)

    cancelled = await runner.cancel(run.run_id, by="alice", reason="wrong environment")
    assert cancelled.status is RunStatus.CANCELLED
    assert cancelled.cancellation is not None
    assert (cancelled.cancellation.by, cancelled.cancellation.reason) == (
        "alice",
        "wrong environment",
    )
    assert cancelled.cancellation.interrupted == ()
    assert cancelled.awaiting_approval == ()
    assert await runner.pending_approvals(run.run_id) == []
    with pytest.raises(ApprovalMismatch):  # a cancelled run cannot be approved into running
        await runner.resolve_approval(
            run.run_id, ApprovalDecision(binding=request.binding, approved=True, approver="bob")
        )
    assert tool.calls == []
    again = await runner.cancel(run.run_id, by="bob")  # terminal: unchanged
    assert again.version == cancelled.version
    assert (await runner.execute(run.run_id)).status is RunStatus.CANCELLED  # nothing runs after


async def test_cancelling_mid_tool_names_the_interrupted_invocation(store: RunStore) -> None:
    started = asyncio.Event()

    async def slow(arguments: dict[str, Any], context: object) -> ToolOutput:
        started.set()
        await asyncio.sleep(30)
        return ToolOutput.text("never")

    tool = FakeTool("migrate", effect=ToolEffect.MUTATING, handler=slow, timeout_s=60)

    class Approve:
        async def review(self, request: Any) -> Any:
            from hollis.runtime.tools import ApprovalDecision

            return ApprovalDecision(binding=request.binding, approved=True, approver="t")

    provider = ScriptedModelProvider([wants(call("migrate", "c1"))])
    runner = Runner(store, provider, tools=Catalog(tool), approver=Approve())
    run = await runner.create(
        RunSpec(model="fake", input="migrate", tools=("migrate",), budget=BUDGET)
    )
    task = asyncio.create_task(runner.execute(run.run_id))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cancelled = await runner.cancel(run.run_id, by="ops", reason="taking too long")
    assert cancelled.cancellation is not None
    assert cancelled.cancellation.interrupted == (cancelled.turns[0].invocations[0].invocation_id,)


def test_fold_rejects_cancellation_naming_tools_that_were_not_running() -> None:
    run = uuid4()
    log = build_log(run, [created(), RunCancelled(reason="x", by="a", interrupted=("bogus",))])
    with pytest.raises(InvalidEventSequence, match="not running"):
        fold(log)


async def test_observers_see_every_durable_event_and_cannot_break_the_run(store: RunStore) -> None:
    seen: list[tuple[EventType, RunStatus]] = []

    async def record(event: EventEnvelope, state: RunState) -> None:
        seen.append((event.event_type, state.status))

    async def broken(event: EventEnvelope, state: RunState) -> None:
        raise RuntimeError("observer bug")

    runner = Runner(store, ScriptedModelProvider([answer()]), observers=(broken, record))
    run = await runner.create(RunSpec(model="fake", input="hi", budget=BUDGET))
    final = await runner.execute(run.run_id)
    assert final.status is RunStatus.COMPLETED
    assert [t for t, _ in seen] == [e.event_type for e in await store.read(run.run_id)]
    assert seen[-1] == (EventType.RUN_COMPLETED, RunStatus.COMPLETED)


async def test_runs_are_listed_newest_first(store: RunStore) -> None:
    from hollis.runtime.store import RunCatalog

    runner = Runner(store, ScriptedModelProvider([answer(), answer()]))
    first = await runner.create(RunSpec(model="fake", input="one", budget=BUDGET))
    await asyncio.sleep(0.002)
    second = await runner.create(RunSpec(model="fake", input="two", budget=BUDGET))
    await runner.execute(second.run_id)
    assert isinstance(store, RunCatalog)
    listings = await store.list_runs()
    assert [r.run_id for r in listings] == [second.run_id, first.run_id]
    assert listings[0].version > 1
    assert listings[0].updated_at >= listings[0].created_at
    assert [r.run_id for r in await store.list_runs(limit=1, offset=1)] == [first.run_id]
