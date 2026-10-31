"""Recovery when a crash lands during the tool lifecycle (ADR-0014).

The dangerous case is a tool that performed an external side effect before the process died,
with no recorded result. It must not be repeated unless the tool is idempotent.
"""

import asyncio
from typing import Any

import pytest

from hollis.adapters.fake_provider import ScriptedModelProvider
from hollis.runtime.events import EventType, ToolOutcome
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import Runner, RunSpec
from hollis.runtime.state import RunState, RunStatus
from hollis.runtime.store import RunStore
from hollis.runtime.tools import (
    ApprovalDecision,
    ApprovalRequest,
    ToolContext,
    ToolEffect,
    ToolOutput,
)
from tests.support.crash import CrashingStore, SimulatedCrash
from tests.support.tools import Catalog, FakeTool, answer, call, results, wants

SPEC = RunSpec(model="fake", input="pay", tools=("pay",), budget=RunBudget(max_output_tokens=64))


class Approve:
    async def review(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(binding=request.binding, approved=True, approver="ops")


class SideEffects:
    """An external system: counts every effect actually applied."""

    def __init__(self) -> None:
        self.applied: list[str] = []
        self.crash_after_effect = True

    async def pay(self, arguments: dict[str, Any], context: ToolContext) -> ToolOutput:
        self.applied.append(context.invocation_id)
        if self.crash_after_effect:
            self.crash_after_effect = False
            raise SimulatedCrash("process died after the payment went through")
        return ToolOutput.text("paid")


def _runner(store: RunStore, provider: ScriptedModelProvider, tool: FakeTool) -> Runner:
    return Runner(store, provider, tools=Catalog(tool), approver=Approve())


def _outcome(state: RunState) -> ToolOutcome | None:
    return state.turns[0].invocations[0].outcome


async def _crash_then_resume(
    store: RunStore, tool: FakeTool
) -> tuple[RunState, ScriptedModelProvider]:
    provider = ScriptedModelProvider([wants(call("pay")), answer("ok")])
    runner = _runner(store, provider, tool)
    run = await runner.create(SPEC)
    with pytest.raises(SimulatedCrash):
        await runner.execute(run.run_id)
    return await _runner(store, provider, tool).execute(run.run_id), provider


async def test_non_idempotent_side_effect_is_never_repeated(store: RunStore) -> None:
    effects = SideEffects()
    tool = FakeTool("pay", effect=ToolEffect.MUTATING, handler=effects.pay)

    final, provider = await _crash_then_resume(store, tool)

    assert final.status is RunStatus.COMPLETED
    assert _outcome(final) is ToolOutcome.AMBIGUOUS
    assert len(effects.applied) == 1  # the payment happened exactly once
    message = results(provider.requests[1])[0].content
    assert "outcome of pay is unknown" in message


async def test_idempotent_tool_is_re_executed_with_the_same_invocation_id(store: RunStore) -> None:
    effects = SideEffects()
    tool = FakeTool("pay", effect=ToolEffect.MUTATING, idempotent=True, handler=effects.pay)

    final, _ = await _crash_then_resume(store, tool)

    assert _outcome(final) is ToolOutcome.SUCCESS
    assert final.turns[0].invocations[0].attempts == 2
    assert len(set(effects.applied)) == 1  # same idempotency key both times
    assert len(effects.applied) == 2


async def test_reconcilable_tool_reports_the_earlier_outcome(store: RunStore) -> None:
    effects = SideEffects()
    tool = FakeTool(
        "pay",
        effect=ToolEffect.MUTATING,
        reconcilable=True,
        handler=effects.pay,
        reconcile_result=ToolOutput.text("payment 42 found: settled"),
    )

    final, provider = await _crash_then_resume(store, tool)

    assert _outcome(final) is ToolOutcome.SUCCESS
    assert (tool.reconciled, len(effects.applied)) == (1, 1)
    assert "settled" in results(provider.requests[1])[0].content


async def test_unreconcilable_outcome_falls_back_to_ambiguous(store: RunStore) -> None:
    effects = SideEffects()
    tool = FakeTool("pay", effect=ToolEffect.MUTATING, reconcilable=True, handler=effects.pay)
    final, _ = await _crash_then_resume(store, tool)
    assert _outcome(final) is ToolOutcome.AMBIGUOUS
    assert (tool.reconciled, len(effects.applied)) == (1, 1)


@pytest.mark.parametrize(
    "crash_after",
    [
        EventType.TOOL_CALL_DECIDED,
        EventType.TOOL_APPROVAL_RESOLVED,
        EventType.TOOL_RESULT_RECORDED,
    ],
)
async def test_crash_between_lifecycle_events_resumes_without_repeating(
    store: RunStore, crash_after: EventType
) -> None:
    effects = SideEffects()
    effects.crash_after_effect = False
    tool = FakeTool("pay", effect=ToolEffect.MUTATING, handler=effects.pay)
    provider = ScriptedModelProvider([wants(call("pay")), answer("ok")])
    crashing = Runner(
        CrashingStore(store, crash_after), provider, tools=Catalog(tool), approver=Approve()
    )
    run = await crashing.create(SPEC)
    with pytest.raises(SimulatedCrash):
        await crashing.execute(run.run_id)

    final = await _runner(store, provider, tool).execute(run.run_id)

    assert final.status is RunStatus.COMPLETED
    assert _outcome(final) is ToolOutcome.SUCCESS
    assert len(effects.applied) == 1


async def test_cancelled_side_effect_is_ambiguous_on_resume(store: RunStore) -> None:
    started = asyncio.Event()

    async def slow_pay(arguments: dict[str, Any], context: ToolContext) -> ToolOutput:
        started.set()
        await asyncio.sleep(10)
        return ToolOutput.text("paid")

    tool = FakeTool("pay", effect=ToolEffect.MUTATING, handler=slow_pay)
    provider = ScriptedModelProvider([wants(call("pay")), answer("ok")])
    runner = _runner(store, provider, tool)
    run = await runner.create(SPEC)
    task = asyncio.create_task(runner.execute(run.run_id))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    final = await _runner(store, provider, tool).execute(run.run_id)
    assert _outcome(final) is ToolOutcome.AMBIGUOUS
    assert len(tool.calls) == 1
