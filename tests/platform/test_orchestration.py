"""Multi-agent orchestration through the platform (ADR-0030, ADR-0031): delegation as durable
child runs, structured reports, parallel and dependent work, plans, permissions that only
narrow, limits, budgets, approvals, cancellation, failures and recovery."""

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from hollis.platform.auth import Principal
from hollis.platform.errors import Conflict, Forbidden, InvalidRequest
from hollis.platform.schemas import (
    AgentInput,
    Approval,
    ApprovalDecisionInput,
    CancelInput,
    Plan,
    PlanStep,
    Role,
    RunPhase,
    StartRun,
)
from hollis.platform.services import Platform
from hollis.runtime.delegation import DelegationOverrides, child_run_id
from hollis.runtime.events import EventType, ToolExecutionStarted, ToolResultRecorded
from hollis.runtime.knowledge import KnowledgeAccess
from hollis.runtime.model import FailureKind, ModelCallError, ModelRequest, ModelResponse
from hollis.runtime.store import load_run
from tests.support.orchestration import (
    FINANCE_SECRET,
    ScriptedAgentModel,
    add_team,
    answer,
    calls,
    delegate,
    first_turn,
    run_lead,
    tool_results,
)
from tests.support.platform import add_principal


async def _pending_approval(platform: Platform, principal: Principal) -> Approval:
    """The first approval that becomes pending (delegated runs ask from their own task)."""
    while True:
        pending = await platform.approvals(principal)
        if pending:
            return pending[0]
        await platform.ws.bus.wait_until(lambda: False, 0.02)


async def test_a_lead_delegates_independent_tasks_concurrently_and_aggregates(
    platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin)
    done = await run_lead(
        platform, admin, "@researcher how do we deploy? @analyst what is the travel budget?"
    )
    assert done.phase is RunPhase.COMPLETED
    assert done.children == 2

    events = await platform.ws.runs.read(done.run_id)
    kinds = [type(e.payload) for e in events]
    starts = [i for i, k in enumerate(kinds) if k is ToolExecutionStarted]
    results = [i for i, k in enumerate(kinds) if k is ToolResultRecorded]
    assert len(starts) == 2
    assert max(starts) < min(results), "both read-only delegations ran at the same time"

    tree = await platform.run_tree(admin, done.run_id)
    assert [n.run.depth for n in tree.nodes] == [0, 1, 1]
    researcher, analyst = tree.nodes[1:]
    assert (researcher.run.agent, analyst.run.agent) == ("researcher", "analyst")
    assert researcher.run.objective == "how do we deploy?"
    for node in (researcher, analyst):
        assert node.report is not None
        assert node.report.outcome == "completed"
        assert node.report.status == "done"
        assert node.report.verification is not None
        assert node.report.verification.supported >= 1
        assert node.report.evidence, "the child's cited evidence travels with its report"
    assert tree.totals.runs == 3
    assert tree.totals.coordinator_tokens > 0
    assert tree.totals.worker_tokens > 0

    detail = await platform.run(admin, done.run_id)
    assert detail.answer is not None
    assert "make ship" in detail.answer
    assert "41000" in detail.answer
    assert detail.grounding is not None
    assert detail.grounding.fabricated == ()
    assert detail.grounding.cited, "the lead cites evidence its children found"


async def test_dependent_work_receives_earlier_reports_and_their_evidence(
    platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin)
    done = await run_lead(
        platform, admin, "@researcher how do we deploy? then @reviewer check the answer"
    )
    assert done.phase is RunPhase.COMPLETED
    tree = await platform.run_tree(admin, done.run_id)
    researcher, reviewer = tree.nodes[1:]
    assert reviewer.inputs == (researcher.run.run_id,)
    assert reviewer.started_at >= (researcher.finished_at or reviewer.started_at)

    review = await platform.run(admin, reviewer.run.run_id)
    assert review.assignment is not None
    assert review.assignment.inputs[0].agent == "researcher"
    assert review.assignment.evidence, "verified evidence is handed down, not retyped"
    assert "<agent-report" in review.input
    assert review.answer is not None
    assert "Reviewed 1 reports" in review.answer
    assert review.grounding is not None
    assert review.grounding.fabricated == (), "citing handed-down evidence is not fabrication"
    assert [step.agent for step in review.lineage] == ["lead"]


async def test_permissions_only_narrow_down_the_tree(platform: Platform, admin: Principal) -> None:
    await add_team(platform, admin)
    olive = await add_principal(
        platform, admin, "olive", knowledge=KnowledgeAccess(collections=("docs",))
    )
    done = await run_lead(
        platform, olive, "@researcher how do we deploy? @analyst what is the travel budget?"
    )
    assert done.phase is RunPhase.COMPLETED
    tree = await platform.run_tree(olive, done.run_id)
    analyst = next(n for n in tree.nodes if n.run.agent == "analyst")
    assert analyst.narrowed == ("knowledge finance",)
    analyst_run = await platform.run(olive, analyst.run.run_id)
    assert analyst_run.knowledge_access is None
    assert analyst_run.summary.created_by == "olive", "delegated runs belong to the root's owner"

    for node in tree.nodes:  # nothing from a collection olive cannot read reached any run
        for event in await platform.ws.runs.read(node.run.run_id):
            assert FINANCE_SECRET not in event.model_dump_json()


async def test_delegating_to_an_agent_that_can_change_things_needs_approval(
    platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin)
    waiting = await run_lead(platform, admin, "@writer write notes.txt: ship on tuesday")
    assert waiting.phase is RunPhase.WAITING_APPROVAL
    [delegation] = await platform.approvals(admin)
    assert delegation.tool == "delegate_writer"
    assert delegation.effect == "mutating"
    await platform.decide(
        admin, waiting.run_id, delegation.invocation_id, ApprovalDecisionInput(approved=True)
    )

    write = await asyncio.wait_for(_pending_approval(platform, admin), timeout=10)
    assert write.tool == "fs_write_file"
    assert write.depth == 1
    assert write.root_run_id == waiting.run_id
    assert write.lineage == ("lead", "writer")
    await platform.decide(
        admin, write.run_id, write.invocation_id, ApprovalDecisionInput(approved=True)
    )
    done = await platform.wait_run(admin, waiting.run_id, timeout_s=10)
    assert done.phase is RunPhase.COMPLETED
    assert (platform.ws.root / "notes.txt").read_text() == "ship on tuesday"


async def test_approving_a_delegated_call_needs_the_permission_and_access(
    platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin)
    olive = await add_principal(platform, admin, "olive")
    vera = await add_principal(platform, admin, "vera", can_approve=True)
    waiting = await run_lead(platform, olive, "@writer write a.txt: x")
    [delegation] = await platform.approvals(admin)
    with pytest.raises(Forbidden):
        await platform.decide(
            olive, waiting.run_id, delegation.invocation_id, ApprovalDecisionInput(approved=True)
        )
    assert await platform.approvals(vera) == [], "vera cannot see olive's runs"


async def test_cancelling_a_run_cancels_what_it_delegated(
    platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin)
    waiting = await run_lead(platform, admin, "@writer write a.txt: x")
    [delegation] = await platform.approvals(admin)
    await platform.decide(
        admin, waiting.run_id, delegation.invocation_id, ApprovalDecisionInput(approved=True)
    )
    child_id = child_run_id(waiting.run_id, delegation.invocation_id)

    await asyncio.wait_for(_pending_approval(platform, admin), timeout=10)
    await platform.cancel_run(admin, waiting.run_id, CancelInput(reason="stop"))
    child = await platform.run(admin, child_id)
    assert child.summary.phase is RunPhase.CANCELLED
    assert child.cancellation is not None
    assert str(waiting.run_id) in child.cancellation.reason
    assert await platform.approvals(admin) == []
    assert not (platform.ws.root / "a.txt").exists()


async def test_limits_refuse_extra_delegations_and_the_parent_continues(
    platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin, delegation=DelegationOverrides(max_children=1))
    done = await run_lead(
        platform, admin, "@researcher how do we deploy? @analyst what is the travel budget?"
    )
    assert done.phase is RunPhase.COMPLETED
    assert done.children == 1
    tree = await platform.run_tree(admin, done.run_id)
    [refused] = tree.refusals
    assert "limit 1" in refused.reason
    detail = await platform.run(admin, done.run_id)
    assert detail.answer is not None
    assert "refused" in detail.answer


def _recursive(request: ModelRequest) -> ModelResponse:
    if first_turn(request) and any(t.name == "delegate_loop" for t in request.tools):
        return calls(delegate("loop", "go one level deeper"))
    return answer("bottom" if first_turn(request) else "relayed")


async def test_self_delegation_stops_at_the_depth_limit(
    platform: Platform, admin: Principal
) -> None:
    platform.ws.register_provider(ScriptedAgentModel("recursive", _recursive))
    await platform.patch_config(admin, {"orchestration": {"max_depth": 3}})
    await platform.create_agent(
        admin, AgentInput(name="loop", model="recursive", delegates=("loop",))
    )
    done = await run_lead(platform, admin, "dig", agent="loop")
    assert done.phase is RunPhase.COMPLETED
    tree = await platform.run_tree(admin, done.run_id)
    assert [n.run.depth for n in tree.nodes] == [0, 1, 2, 3]
    deepest = await platform.run(admin, tree.nodes[-1].run.run_id)
    assert "delegate_loop" not in deepest.tools, "no delegate tool at the depth limit"


async def test_a_token_budget_bounds_the_whole_tree(platform: Platform, admin: Principal) -> None:
    def lead(request: ModelRequest) -> ModelResponse:
        results = tool_results(request)
        if results and "refused" in results[-1]:
            return answer(f"partial: {len(results) - 1} tasks done before the budget ran low")
        return calls(delegate("worker", f"task {len(results)}", call_id=f"d{len(results)}"))

    platform.ws.register_provider(ScriptedAgentModel("costly-lead", lead, tokens=1000))
    platform.ws.register_provider(
        ScriptedAgentModel("costly", lambda r: answer("done"), tokens=1000)
    )
    await platform.create_agent(admin, AgentInput(name="worker", model="costly"))
    await platform.create_agent(
        admin,
        AgentInput(
            name="boss",
            model="costly-lead",
            delegates=("worker",),
            delegation=DelegationOverrides(token_budget=3500),
        ),
    )
    done = await run_lead(platform, admin, "work", agent="boss")
    # boss 1000, worker 1000, boss 1000 (3000 >= 75% of 3500: no new delegation), boss reports.
    assert done.phase is RunPhase.COMPLETED
    assert done.children == 1
    tree = await platform.run_tree(admin, done.run_id)
    [refused] = tree.refusals
    assert "delegated work stops at 75%" in refused.reason
    assert tree.totals.input_tokens == 4000, "at most one call in flight overshoots the budget"
    detail = await platform.run(admin, done.run_id)
    assert detail.answer == "partial: 1 tasks done before the budget ran low"


async def test_model_calls_stop_at_the_full_token_budget(
    platform: Platform, admin: Principal
) -> None:
    def lead(request: ModelRequest) -> ModelResponse:
        results = tool_results(request)
        return calls(delegate("worker", "go", call_id=f"d{len(results)}"))  # never stops

    platform.ws.register_provider(ScriptedAgentModel("greedy", lead, tokens=1000))
    platform.ws.register_provider(
        ScriptedAgentModel("costly", lambda r: answer("done"), tokens=1000)
    )
    await platform.create_agent(admin, AgentInput(name="worker", model="costly"))
    await platform.create_agent(
        admin,
        AgentInput(
            name="boss",
            model="greedy",
            delegates=("worker",),
            delegation=DelegationOverrides(token_budget=3500),
        ),
    )
    done = await run_lead(platform, admin, "work", agent="boss")
    assert done.phase is RunPhase.FAILED
    detail = await platform.run(admin, done.run_id)
    assert detail.failure is not None
    assert detail.failure.reason == "budget_exhausted"
    assert (
        "token budget of 3500 for this orchestration is used up (4000 tokens)"
        in detail.failure.message
    )


async def test_a_failed_child_becomes_a_report_the_parent_can_act_on(
    platform: Platform, admin: Principal
) -> None:
    async def broken(request: ModelRequest) -> ModelResponse:
        raise ModelCallError(FailureKind.AUTHENTICATION, "invalid api key")

    platform.ws.register_provider(ScriptedAgentModel("broken", broken))

    def lead(request: ModelRequest) -> ModelResponse:
        if first_turn(request):
            return calls(delegate("flaky", "look it up"))
        return answer(
            "the specialist failed: "
            + ("failed" if "outcome failed" in tool_results(request)[0] else "?")
        )

    platform.ws.register_provider(ScriptedAgentModel("lead-model", lead))
    await platform.create_agent(admin, AgentInput(name="flaky", model="broken"))
    await platform.create_agent(
        admin, AgentInput(name="boss", model="lead-model", delegates=("flaky",))
    )
    done = await run_lead(platform, admin, "work", agent="boss")
    assert done.phase is RunPhase.COMPLETED
    tree = await platform.run_tree(admin, done.run_id)
    child = tree.nodes[1]
    assert child.run.phase is RunPhase.FAILED
    assert child.report is not None
    assert child.report.outcome == "failed"
    assert child.report.reason is not None
    assert "provider_error" in child.report.reason
    detail = await platform.run(admin, done.run_id)
    assert detail.answer == "the specialist failed: failed"


async def test_inputs_must_be_finished_delegations_of_the_same_run(
    platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin)
    other = await run_lead(platform, admin, "@researcher how do we deploy?")

    def lead(request: ModelRequest) -> ModelResponse:
        if first_turn(request):
            return calls(
                delegate("reviewer", "check it", inputs=[str(other.children and other.run_id)])
            )
        return answer("done")

    platform.ws.register_provider(ScriptedAgentModel("lead-model", lead))
    await platform.create_agent(
        admin, AgentInput(name="boss", model="lead-model", delegates=("reviewer",))
    )
    done = await run_lead(platform, admin, "work", agent="boss")
    tree = await platform.run_tree(admin, done.run_id)
    [refused] = tree.refusals
    assert "not a finished delegation of this run" in refused.reason
    assert done.children == 0


async def test_a_plan_runs_waves_passes_results_and_skips_what_cannot_run(
    platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin)

    async def broken(request: ModelRequest) -> ModelResponse:
        raise ModelCallError(FailureKind.AUTHENTICATION, "no key")

    platform.ws.register_provider(ScriptedAgentModel("broken", broken))
    await platform.create_agent(admin, AgentInput(name="flaky", model="broken"))
    plan = Plan(
        steps=(
            PlanStep(id="deploy", agent="researcher", objective="how do we deploy?"),
            PlanStep(id="budget", agent="analyst", objective="what is the travel budget?"),
            PlanStep(
                id="review",
                agent="reviewer",
                objective="check both",
                depends_on=("deploy", "budget"),
            ),
            PlanStep(id="extra", agent="flaky", objective="anything else?"),
            PlanStep(
                id="after-extra", agent="reviewer", objective="check it", depends_on=("extra",)
            ),
        )
    )
    started = await platform.start_run(
        admin, StartRun(input="Prepare the release brief", plan=plan)
    )
    done = await platform.wait_run(admin, started.run_id, timeout_s=20)
    assert done.phase is RunPhase.COMPLETED
    assert done.model == "workflow"
    detail = await platform.run(admin, done.run_id)
    assert detail.answer is not None
    assert detail.answer.startswith("Plan finished: 3 of 5 steps completed, 1 did not, 1 skipped.")
    assert "## after-extra (reviewer): skipped" in detail.answer
    assert "make ship" in detail.answer
    assert detail.summary.usage.input_tokens == 0, "the coordinator is not a model"
    assert len(detail.turns) == 3, "two waves of delegations, then the answer"
    tree = await platform.run_tree(admin, done.run_id)
    review = next(n for n in tree.nodes if n.run.objective == "check both")
    by_objective = {n.run.objective: n.run.run_id for n in tree.nodes}
    assert set(review.inputs) == {
        by_objective["how do we deploy?"],
        by_objective["what is the travel budget?"],
    }


async def test_a_plan_can_hand_off_to_its_last_step(platform: Platform, admin: Principal) -> None:
    await add_team(platform, admin)
    plan = Plan(
        steps=(
            PlanStep(id="find", agent="researcher", objective="how do we deploy?"),
            PlanStep(id="check", agent="reviewer", objective="check it", depends_on=("find",)),
        ),
        answer="last",
    )
    started = await platform.start_run(admin, StartRun(input="Brief", plan=plan))
    done = await platform.wait_run(admin, started.run_id, timeout_s=20)
    detail = await platform.run(admin, done.run_id)
    assert detail.answer is not None
    assert detail.answer.startswith("[demo] Reviewed 1 reports")
    assert detail.answer.endswith("(Handed off to `reviewer`, step check.)")


async def test_disagreements_are_kept_and_reported(platform: Platform, admin: Principal) -> None:
    for name, reply in (("optimist", "14 days"), ("pessimist", "30 days")):
        text = f'It is {reply}.\n\n```json\n{{"status": "done", "answer": "{reply}"}}\n```'
        platform.ws.register_provider(ScriptedAgentModel(name, lambda r, t=text: answer(t)))
        await platform.create_agent(admin, AgentInput(name=name, model=name))
    question = "What is the refund window?"
    plan = Plan(
        steps=(
            PlanStep(id="a", agent="optimist", objective=question),
            PlanStep(id="b", agent="pessimist", objective=question),
        )
    )
    started = await platform.start_run(admin, StartRun(input="Refunds", plan=plan))
    done = await platform.wait_run(admin, started.run_id, timeout_s=20)
    detail = await platform.run(admin, done.run_id)
    assert detail.answer is not None
    assert "Disagreements (kept as reported, not resolved):" in detail.answer
    assert "a: 14 days; b: 30 days" in detail.answer
    tree = await platform.run_tree(admin, done.run_id)
    [disagreement] = tree.disagreements
    assert disagreement.answers == ("14 days", "30 days")


async def test_agents_delegates_are_validated(platform: Platform, admin: Principal) -> None:
    with pytest.raises(InvalidRequest, match="no agents named ghost"):
        await platform.create_agent(
            admin, AgentInput(name="lead", model="demo", delegates=("ghost",))
        )
    await platform.create_agent(admin, AgentInput(name="helper", model="demo"))
    await platform.create_agent(admin, AgentInput(name="lead", model="demo", delegates=("helper",)))
    with pytest.raises(Conflict, match="delegate to 'helper'"):
        await platform.delete_agent(admin, "helper")
    tools = {t.name: t for t in await platform.tools(admin)}
    assert tools["delegate_helper"].source == "agents"
    assert tools["delegate_helper"].effect == "read_only"
    assert tools["delegate_helper"].policy == "allow"


async def test_plans_are_for_operators_and_name_existing_agents(
    platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin)
    viewer = await add_principal(platform, admin, "vic", role=Role.VIEWER)
    plan = Plan(steps=(PlanStep(id="a", agent="researcher", objective="how do we deploy?"),))
    with pytest.raises(Forbidden):
        await platform.start_run(viewer, StartRun(input="x", plan=plan))
    ghost = Plan(steps=(PlanStep(id="a", agent="ghost", objective="x"),))
    with pytest.raises(Exception, match="no agent 'ghost'"):
        await platform.start_run(admin, StartRun(input="x", plan=ghost))
    with pytest.raises(ValueError, match="cycle"):
        Plan(
            steps=(
                PlanStep(id="a", agent="x", objective="o", depends_on=("b",)),
                PlanStep(id="b", agent="x", objective="o", depends_on=("a",)),
            )
        )


async def test_runs_list_can_leave_out_delegated_runs(platform: Platform, admin: Principal) -> None:
    await add_team(platform, admin)
    done = await run_lead(platform, admin, "@researcher how do we deploy?")
    everything = await platform.runs(admin)
    roots = await platform.runs(admin, roots=True)
    assert everything.total == 2
    assert [r.run_id for r in roots.items] == [done.run_id]
    assert roots.items[0].children == 1


async def test_an_interrupted_delegation_reattaches_to_its_child_after_restart(
    tmp_path: Path, platform: Platform, admin: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hollis.adapters.demo_model import DemoModelProvider

    await add_team(platform, admin)
    entered = asyncio.Event()
    original = DemoModelProvider.generate

    async def child_hangs_once(self: DemoModelProvider, request: Any) -> Any:
        if (
            request.instructions
            and "Report protocol" in request.instructions
            and not entered.is_set()
        ):
            entered.set()
            await asyncio.sleep(3600)  # the process stops while the child is mid-call
        return await original(self, request)

    monkeypatch.setattr(DemoModelProvider, "generate", child_hangs_once)
    started = await platform.start_run(
        admin, StartRun(input="@researcher how do we deploy?", agent="lead")
    )
    await asyncio.wait_for(entered.wait(), timeout=10)
    await platform.close()

    from hollis.platform.services import Platform as Reopened

    reopened = await Reopened.open(tmp_path)
    try:
        recovered = await reopened.ws.recover()
        assert started.run_id in recovered
        done = await reopened.wait_run(admin, started.run_id, timeout_s=20)
        assert done.phase is RunPhase.COMPLETED
        assert done.children == 1, "no second child was created"
        parent = await load_run(reopened.ws.runs, started.run_id)
        [invocation] = parent.turns[0].invocations
        assert invocation.attempts == 2, "re-executed once, attaching to the same child"
        child = await reopened.run(admin, child_run_id(started.run_id, invocation.invocation_id))
        assert child.summary.phase is RunPhase.COMPLETED
        assert [
            a.failure_kind for a in child.turns[0].model_call.attempts if a.outcome == "failed"
        ] == ["interrupted"]
        events = await reopened.ws.runs.read(started.run_id)
        assert sum(1 for e in events if e.event_type is EventType.TOOL_RESULT_RECORDED) == 1
    finally:
        await reopened.close()


async def test_children_cannot_outlive_a_delegation_that_stopped_waiting(
    platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin, delegation=DelegationOverrides(wait_s=1.0))
    waiting = await run_lead(platform, admin, "@writer write a.txt: x")
    [delegation] = await platform.approvals(admin)
    await platform.decide(
        admin, waiting.run_id, delegation.invocation_id, ApprovalDecisionInput(approved=True)
    )
    done = await platform.wait_run(admin, waiting.run_id, timeout_s=10)
    assert done.phase is RunPhase.COMPLETED
    tree = await platform.run_tree(admin, done.run_id)
    child = tree.nodes[1]
    assert child.run.phase is RunPhase.CANCELLED
    assert child.report is not None
    assert child.report.outcome == "timed_out"
    assert child.report.reason is not None
    assert "the delegating run stopped waiting after 1s" in child.report.reason
    assert await platform.approvals(admin) == []


async def test_unknown_runs_have_no_tree(platform: Platform, admin: Principal) -> None:
    from hollis.platform.errors import NotFound

    with pytest.raises(NotFound):
        await platform.run_tree(admin, uuid4())


async def test_concurrent_children_cannot_spend_the_coordinators_reserve(
    platform: Platform, admin: Principal
) -> None:
    """Four children call at once: each admitted call reserves its estimated input, so they
    stop within the delegated share and the lead keeps enough budget to report."""

    def lead(request: ModelRequest) -> ModelResponse:
        if first_turn(request):
            return calls(*(delegate("worker", f"task {i}", call_id=f"d{i}") for i in range(4)))
        return answer(f"{sum('outcome completed' in r for r in tool_results(request))} done")

    platform.ws.register_provider(ScriptedAgentModel("lead-model", lead, tokens=100))
    platform.ws.register_provider(ScriptedAgentModel("slow", lambda r: answer("ok"), tokens=900))
    await platform.create_agent(admin, AgentInput(name="worker", model="slow"))
    await platform.create_agent(
        admin,
        AgentInput(
            name="boss",
            model="lead-model",
            delegates=("worker",),
            delegation=DelegationOverrides(token_budget=2000),
        ),
    )
    done = await run_lead(platform, admin, "go", agent="boss")
    assert done.phase is RunPhase.COMPLETED, "the lead's reserve survived"
    tree = await platform.run_tree(admin, done.run_id)
    workers = [n for n in tree.nodes if n.run.depth == 1]
    assert len(workers) + len(tree.refusals) == 4
    stopped = [
        n for n in workers if n.report is not None and n.report.outcome == "budget_exhausted"
    ]
    assert stopped or tree.refusals, "delegated work was stopped at its share"
    work = sum(n.run.usage.input_tokens for n in workers)
    assert work <= 0.75 * 2000 + 900, "children overshoot their share by at most one call"
    assert platform.ws.orchestrator._reserved == {}, "every reservation was released"  # pyright: ignore[reportPrivateUsage]


async def test_recovery_cancels_orphans_instead_of_resuming_them(
    tmp_path: Path, platform: Platform, admin: Principal
) -> None:
    """The process stopped after the parent was cancelled but before its child was."""
    await add_team(platform, admin)
    waiting = await run_lead(platform, admin, "@writer write a.txt: x")
    [delegation] = await platform.approvals(admin)
    await platform.decide(
        admin, waiting.run_id, delegation.invocation_id, ApprovalDecisionInput(approved=True)
    )
    child_id = child_run_id(waiting.run_id, delegation.invocation_id)
    await asyncio.wait_for(_pending_approval(platform, admin), timeout=10)
    await platform.ws.supervisor.cancel(waiting.run_id, by="admin", reason="stop")  # no cascade
    await platform.close()

    from hollis.platform.services import Platform as Reopened

    reopened = await Reopened.open(tmp_path)
    try:
        assert child_id not in await reopened.ws.recover()
        child = await reopened.run(admin, child_id)
        assert child.summary.phase is RunPhase.CANCELLED
        assert child.cancellation is not None
        assert "had already finished" in child.cancellation.reason
        assert await reopened.approvals(admin) == []
    finally:
        await reopened.close()


async def test_an_execution_error_releases_the_runs_budget_reservation(
    platform: Platform, admin: Principal
) -> None:
    async def crashes(request: ModelRequest) -> ModelResponse:
        raise RuntimeError("a bug in a provider")  # not a classified ModelCallError

    platform.ws.register_provider(ScriptedAgentModel("buggy", crashes))
    await platform.create_agent(admin, AgentInput(name="worker", model="demo"))
    await platform.create_agent(
        admin,
        AgentInput(
            name="boss",
            model="buggy",
            delegates=("worker",),
            delegation=DelegationOverrides(token_budget=5000),
        ),
    )
    stopped = await run_lead(platform, admin, "go", agent="boss")
    assert stopped.phase is RunPhase.INTERRUPTED
    assert stopped.error is not None
    assert "a bug in a provider" in stopped.error
    assert platform.ws.orchestrator._reserved == {}  # pyright: ignore[reportPrivateUsage]


async def test_agent_edits_that_would_strand_running_delegations_are_refused(
    platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin)
    waiting = await run_lead(platform, admin, "@writer write a.txt: x")  # uses delegate_writer
    assert waiting.phase is RunPhase.WAITING_APPROVAL
    writer = await platform.agent(admin, "writer")
    spec = AgentInput.model_validate(writer.model_dump(include=set(AgentInput.model_fields)))
    await platform.update_agent(
        admin, "writer", spec.model_copy(update={"instructions": "Be brief."})
    )
    with pytest.raises(Conflict, match="unfinished runs use delegate_writer"):
        await platform.update_agent(admin, "writer", spec.model_copy(update={"description": "new"}))
    await platform.cancel_run(admin, waiting.run_id, CancelInput(reason="done"))
    await platform.update_agent(admin, "writer", spec.model_copy(update={"description": "new"}))


async def test_cancellation_and_timeouts_reach_every_level(
    platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin)
    await platform.create_agent(
        admin, AgentInput(name="manager", model="demo", delegates=("writer",))
    )
    await platform.update_agent(
        admin,
        "lead",
        AgentInput.model_validate(
            (await platform.agent(admin, "lead")).model_dump(include=set(AgentInput.model_fields))
            | {"delegates": ("manager",)}
        ),
    )
    # lead -> manager (delegate_manager is mutating: approve) -> writer (approve) -> write (waits)
    root = await run_lead(platform, admin, "@manager @writer write deep.txt: x")
    for _ in range(2):
        pending = await asyncio.wait_for(_pending_approval(platform, admin), timeout=10)
        await platform.decide(
            admin, pending.run_id, pending.invocation_id, ApprovalDecisionInput(approved=True)
        )
        await asyncio.sleep(0)
    deepest = await asyncio.wait_for(_pending_approval(platform, admin), timeout=10)
    assert deepest.depth == 2
    await platform.cancel_run(admin, root.run_id, CancelInput(reason="stop all"))
    tree = await platform.run_tree(admin, root.run_id)
    assert {n.run.phase for n in tree.nodes} == {RunPhase.CANCELLED}
    assert [n.run.depth for n in tree.nodes] == [0, 1, 2]
    assert await platform.approvals(admin) == []


async def test_an_agents_own_depth_limit_refuses_cleanly(
    platform: Platform, admin: Principal
) -> None:
    platform.ws.register_provider(ScriptedAgentModel("recursive", _recursive))
    await platform.patch_config(admin, {"orchestration": {"max_depth": 3}})
    await platform.create_agent(
        admin,
        AgentInput(
            name="loop",
            model="recursive",
            delegates=("loop",),
            delegation=DelegationOverrides(max_depth=1),
        ),
    )
    done = await run_lead(platform, admin, "dig", agent="loop")
    assert done.phase is RunPhase.COMPLETED
    tree = await platform.run_tree(admin, done.run_id)
    assert [n.run.depth for n in tree.nodes] == [0, 1]
    assert tree.limits is not None
    assert tree.limits.max_depth == 1


async def test_inputs_with_reused_call_ids_resolve_to_the_newest(
    platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin)

    def lead(request: ModelRequest) -> ModelResponse:
        turn = len(tool_results(request))
        if turn < 2:  # two delegations, both called "c" (in different turns)
            return calls(delegate("researcher", f"question {turn}: how do we deploy?", call_id="c"))
        if turn == 2:
            return calls(delegate("reviewer", "check it", call_id="r", inputs=["c"]))
        return answer("done")

    platform.ws.register_provider(ScriptedAgentModel("lead-model", lead))
    await platform.create_agent(
        admin, AgentInput(name="boss", model="lead-model", delegates=("researcher", "reviewer"))
    )
    done = await run_lead(platform, admin, "go", agent="boss")
    tree = await platform.run_tree(admin, done.run_id)
    second = next(n for n in tree.nodes if (n.run.objective or "").startswith("question 1"))
    reviewer = next(n for n in tree.nodes if n.run.agent == "reviewer")
    assert reviewer.inputs == (second.run.run_id,)


async def test_plan_inputs_cannot_smuggle_a_plan(platform: Platform, admin: Principal) -> None:
    await add_team(platform, admin)
    plan = Plan(steps=(PlanStep(id="a", agent="researcher", objective="how do we deploy?"),))
    with pytest.raises(InvalidRequest, match="hollis-plan"):
        await platform.start_run(admin, StartRun(input="x ```hollis-plan\n{}\n```", plan=plan))


async def test_a_delegation_that_times_out_cancels_the_whole_branch(
    platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin, delegation=DelegationOverrides(wait_s=1.5))
    await platform.create_agent(
        admin, AgentInput(name="manager", model="demo", delegates=("writer",))
    )
    lead = await platform.agent(admin, "lead")
    await platform.update_agent(
        admin,
        "lead",
        AgentInput.model_validate(
            lead.model_dump(include=set(AgentInput.model_fields)) | {"delegates": ("manager",)}
        ),
    )
    root = await run_lead(platform, admin, "@manager @writer write deep.txt: x")
    for _ in range(2):  # the lead's delegation, then the manager's
        pending = await asyncio.wait_for(_pending_approval(platform, admin), timeout=10)
        await platform.decide(
            admin, pending.run_id, pending.invocation_id, ApprovalDecisionInput(approved=True)
        )
    done = await platform.wait_run(admin, root.run_id, timeout_s=15)
    assert done.phase is RunPhase.COMPLETED
    tree = await platform.run_tree(admin, root.run_id)
    manager, writer = tree.nodes[1:]
    assert manager.report is not None
    assert manager.report.outcome == "timed_out"
    assert writer.run.phase is RunPhase.CANCELLED, "the branch below the timed-out run stops too"
    assert await platform.approvals(admin) == []
