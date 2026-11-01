"""Runtime facts for delegation (ADR-0030, ADR-0031): lineage in RunCreated, caller-chosen run
ids, per-run token limits, the model-call guard and delegation depth in policy."""

import json
from typing import Any
from uuid import uuid4

import pytest

from hollis.adapters.fake_provider import ScriptedModelProvider
from hollis.adapters.memory_store import InMemoryRunStore
from hollis.runtime.delegation import (
    Assignment,
    DelegationLimits,
    DelegationOverrides,
    Lineage,
    child_run_id,
)
from hollis.runtime.events import (
    FailureReason,
    ModelCallStarted,
    ModelResponseRecorded,
    ToolCallDecided,
    ToolOutcome,
    ToolResultRecorded,
    deserialize_event,
    serialize_event,
)
from hollis.runtime.model import ModelResponse, Usage
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import Runner, RunSpec
from hollis.runtime.state import InvalidEventSequence, RunState, RunStatus, fold
from hollis.runtime.store import ConcurrencyConflict
from hollis.runtime.tools import (
    Decision,
    PolicyRequest,
    ToolEffect,
    ToolTrust,
)
from hollis.tools.policy import RulePolicy, allow, deny
from tests.support.events import build_log, created
from tests.support.tools import Catalog, FakeTool, answer, call, wants

BUDGET = RunBudget(max_output_tokens=256)


def _lineage(**overrides: Any) -> Lineage:
    root = uuid4()
    fields: dict[str, Any] = {
        "root_run_id": root,
        "parent_run_id": root,
        "parent_invocation_id": f"{root}/turn/0/call/0",
        "depth": 1,
    }
    return Lineage(**(fields | overrides))


def _spec(**overrides: Any) -> RunSpec:
    fields: dict[str, Any] = {"model": "fake", "input": "do it", "budget": BUDGET}
    return RunSpec(**(fields | overrides))


async def _no_sleep(_: float) -> None:
    return None


def test_child_run_ids_are_deterministic_per_parent_invocation() -> None:
    parent = uuid4()
    first = child_run_id(parent, f"{parent}/turn/0/call/0")
    assert first == child_run_id(parent, f"{parent}/turn/0/call/0")
    assert first != child_run_id(parent, f"{parent}/turn/0/call/1")
    assert first != child_run_id(uuid4(), f"{parent}/turn/0/call/0")


def test_limits_narrow_field_by_field_and_never_widen() -> None:
    parent = DelegationLimits(max_depth=3, max_children=4, max_runs=10, token_budget=5000)
    child = DelegationLimits(max_depth=5, max_children=8, max_runs=4, token_budget=None, wait_s=60)
    narrowed = parent.narrow(child)
    assert narrowed == DelegationLimits(
        max_depth=3, max_children=4, max_runs=4, token_budget=5000, wait_s=60
    )
    assert parent.narrow(None) == parent


def test_overrides_narrow_only_the_fields_they_set() -> None:
    workspace = DelegationLimits(max_depth=4, max_runs=100, wait_s=3000)
    narrowed = workspace.narrow(DelegationOverrides(token_budget=200_000, max_runs=500))
    assert narrowed == DelegationLimits(
        max_depth=4, max_runs=100, wait_s=3000, token_budget=200_000
    ), "unset fields inherit, and a larger value never widens"


def test_run_created_v5_payloads_upcast_without_lineage() -> None:
    run_id = uuid4()
    [event] = build_log(run_id, [created()])
    stored = json.loads(serialize_event(event))
    stored["schema_version"] = 5
    for field in ("lineage", "assignment", "delegation"):
        stored["payload"].pop(field)
    stored["payload"]["budget"].pop("max_total_tokens")
    state = fold([deserialize_event(json.dumps(stored))])
    assert state.lineage is None
    assert state.depth == 0
    assert state.budget.max_total_tokens is None


def test_fold_records_lineage_assignment_and_limits() -> None:
    lineage = _lineage()
    assignment = Assignment(agent="researcher", objective="find the rollback steps")
    limits = DelegationLimits(max_depth=2)
    payload = created(lineage=lineage, assignment=assignment, delegation=limits)
    state = fold(build_log(uuid4(), [payload]))
    assert (state.lineage, state.assignment, state.delegation) == (lineage, assignment, limits)
    assert state.depth == 1


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"assignment": Assignment(agent="a", objective="x")}, "needs a parent"),
        ({"lineage": "self"}, "own parent"),
        ({"lineage": "depth1-other-root"}, "parent is its root"),
        ({"lineage": "deep", "delegation": DelegationLimits(max_depth=1)}, "exceeds max_depth"),
    ],
)
def test_fold_rejects_inconsistent_lineage(overrides: dict[str, Any], message: str) -> None:
    run_id = uuid4()
    lineage = overrides.get("lineage")
    if lineage == "self":
        overrides["lineage"] = _lineage(root_run_id=run_id, parent_run_id=run_id)
    elif lineage == "depth1-other-root":
        overrides["lineage"] = _lineage(root_run_id=uuid4())
    elif lineage == "deep":
        overrides["lineage"] = _lineage(parent_run_id=uuid4(), depth=2)
    with pytest.raises(InvalidEventSequence, match=message):
        fold(build_log(run_id, [created(**overrides)]))


async def test_create_with_a_chosen_id_refuses_a_second_creation() -> None:
    store = InMemoryRunStore()
    runner = Runner(store, ScriptedModelProvider([answer()]), sleep=_no_sleep)
    run_id = uuid4()
    state = await runner.create(_spec(lineage=_lineage()), run_id=run_id)
    assert state.run_id == run_id
    assert state.depth == 1
    with pytest.raises(ConcurrencyConflict):
        await runner.create(_spec(lineage=_lineage()), run_id=run_id)
    assert len(await store.read(run_id)) == 1


def _usage(tokens: int) -> Usage:
    return Usage(input_tokens=tokens, output_tokens=0)


async def test_max_total_tokens_stops_new_model_calls() -> None:
    echo = FakeTool("echo")
    script = [
        wants(call("echo", "c1")).model_copy(update={"usage": _usage(600)}),
        wants(call("echo", "c2")).model_copy(update={"usage": _usage(600)}),
        answer("never"),
    ]
    provider = ScriptedModelProvider(script)
    runner = Runner(InMemoryRunStore(), provider, tools=Catalog(echo), sleep=_no_sleep)
    budget = BUDGET.model_copy(update={"max_total_tokens": 1000})
    state = await runner.execute(
        (await runner.create(_spec(tools=("echo",), budget=budget))).run_id
    )
    assert state.status is RunStatus.FAILED
    assert state.failure is not None
    assert state.failure.reason is FailureReason.BUDGET_EXHAUSTED
    assert "max_total_tokens 1000" in state.failure.message
    assert provider.invocations == 2  # the third call never started
    assert len(echo.calls) == 2


def test_fold_rejects_a_model_call_after_the_token_budget() -> None:
    run_id = uuid4()
    budget = BUDGET.model_copy(update={"max_total_tokens": 10})
    op0 = f"{run_id}/model/0"
    payloads = [
        created(budget=budget, tools=()),
        ModelCallStarted(operation_id=op0, attempt=1),
        ModelResponseRecorded(
            operation_id=op0,
            attempt=1,
            response=ModelResponse(content="", usage=_usage(10), tool_calls=(call("x"),)),
        ),
    ]
    invocation = f"{run_id}/turn/0/call/0"
    payloads += [
        ToolCallDecided(
            invocation_id=invocation,
            turn=0,
            index=0,
            call_id="call_1",
            tool="x",
            decision=Decision.INVALID,
            reason="unknown tool",
        ),
        ToolResultRecorded(invocation_id=invocation, outcome=ToolOutcome.INVALID),
    ]
    assert fold(build_log(run_id, payloads)).token_budget_reached
    with pytest.raises(InvalidEventSequence, match="max_total_tokens 10 reached"):
        fold(
            build_log(
                run_id, [*payloads, ModelCallStarted(operation_id=f"{run_id}/model/1", attempt=1)]
            )
        )


class _Guard:
    def __init__(self, refusal: str | None = None, *, error: bool = False) -> None:
        self.refusal = refusal
        self.error = error
        self.seen: list[RunState] = []

    async def refuse(self, state: RunState) -> str | None:
        self.seen.append(state)
        if self.error:
            raise RuntimeError("database is locked")
        return self.refusal


async def test_a_guard_refusal_fails_the_run_before_any_model_call() -> None:
    provider = ScriptedModelProvider([answer()])
    guard = _Guard("orchestration token budget of 100 reached")
    runner = Runner(InMemoryRunStore(), provider, sleep=_no_sleep, guard=guard)
    state = await runner.execute((await runner.create(_spec())).run_id)
    assert state.failure is not None
    assert state.failure.reason is FailureReason.BUDGET_EXHAUSTED
    assert state.failure.message == "orchestration token budget of 100 reached"
    assert provider.invocations == 0


async def test_a_guard_that_fails_refuses_rather_than_allows() -> None:
    provider = ScriptedModelProvider([answer()])
    runner = Runner(InMemoryRunStore(), provider, sleep=_no_sleep, guard=_Guard(error=True))
    state = await runner.execute((await runner.create(_spec())).run_id)
    assert state.status is RunStatus.FAILED
    assert state.failure is not None
    assert "could not be checked" in state.failure.message
    assert provider.invocations == 0


async def test_an_allowing_guard_is_asked_before_every_model_call() -> None:
    echo = FakeTool("echo")
    guard = _Guard()
    provider = ScriptedModelProvider([wants(call("echo")), answer()])
    runner = Runner(InMemoryRunStore(), provider, tools=Catalog(echo), sleep=_no_sleep, guard=guard)
    state = await runner.execute((await runner.create(_spec(tools=("echo",)))).run_id)
    assert state.status is RunStatus.COMPLETED
    assert len(guard.seen) == 2


def _request(depth: int) -> PolicyRequest:
    tool = FakeTool("fs_write_file", effect=ToolEffect.MUTATING).descriptor.ref
    return PolicyRequest(run_id=uuid4(), invocation_id="i", tool=tool, arguments={}, depth=depth)


def test_rules_can_match_delegated_runs_only() -> None:
    policy = RulePolicy.of(
        deny("fs_*", delegated=True, name="no-writes-below"),
        allow("fs_*", delegated=False, name="writes-at-root"),
    )
    assert policy.decide(_request(0)).decision is Decision.ALLOW
    assert policy.decide(_request(2)).decision is Decision.DENY


async def test_the_runner_gives_policy_the_runs_depth() -> None:
    seen: list[int] = []

    class Recording:
        def decide(self, request: PolicyRequest) -> Any:
            seen.append(request.depth)
            return RulePolicy.of(allow()).decide(request)

    echo = FakeTool("echo", trust=ToolTrust.TRUSTED)
    provider = ScriptedModelProvider([wants(call("echo")), answer()])
    runner = Runner(
        InMemoryRunStore(), provider, tools=Catalog(echo), policy=Recording(), sleep=_no_sleep
    )
    spec = _spec(tools=("echo",), lineage=_lineage(parent_run_id=uuid4(), depth=2))
    await runner.execute((await runner.create(spec)).run_id)
    assert seen == [2]
