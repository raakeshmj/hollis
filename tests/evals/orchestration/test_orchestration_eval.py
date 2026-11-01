"""Orchestration evaluation (offline, deterministic simulated agents on the real platform).
See docs/architecture/orchestration-evaluation.md; regenerate the tables with
`uv run python -m tests.evals.orchestration.report`.

These assertions pin what the evaluation found, including where multi-agent loses. Wall-clock
latency is reported but not asserted (it depends on machine load).
"""

from pathlib import Path

from tests.evals.orchestration import harness
from tests.evals.orchestration.harness import Outcome


def _by(outcomes: list[Outcome]) -> dict[str, Outcome]:
    return {o.condition: o for o in outcomes}


async def test_independent_breadth(tmp_path: Path) -> None:
    o = _by(await harness.independent_breadth(tmp_path))
    single8, single16 = o["single agent, 8 turns"], o["single agent, 16 turns"]
    parallel, lead, plan = (
        o["single agent, parallel tool calls"],
        o["lead + 4 specialists"],
        o["plan: 4 specialists"],
    )
    assert single8.phase == "failed", "one search per turn does not fit 8 turns"
    assert single16.success
    assert parallel.success
    assert plan.success
    assert plan.supported == 8, "every fact cited and verified through the delegation reports"
    assert parallel.tokens < plan.tokens < single16.tokens, "isolation beats one long context"
    assert lead.correct >= 7
    assert lead.coordination > 0
    assert lead.tokens > plan.tokens, "a model coordinator costs tokens a plan does not"


async def test_dependent_pipeline(tmp_path: Path) -> None:
    o = _by(await harness.dependent_pipeline(tmp_path))
    single, chained, naive = (
        o["single agent"],
        o["plan: chained, results passed"],
        o["plan: naive parallel split"],
    )
    assert single.success
    assert chained.success, "passing results by reference keeps the dependency"
    assert not naive.success, "splitting dependent work loses the dependency"
    assert chained.tokens > single.tokens, "no gain from multiple agents on sequential work"


async def test_conflicting_sources(tmp_path: Path) -> None:
    single, multi = await harness.conflicting_sources(tmp_path)
    assert multi.extra["surfaced"] is True
    assert multi.success, "the reviewer's conclusion names the newer value only"
    assert single.extra["surfaced"] is False, "one agent sees one answer and moves on"


async def test_agent_failure_is_isolated_and_reported(tmp_path: Path) -> None:
    o = _by(await harness.agent_failure(tmp_path))
    assert o["single agent, its model fails"].correct == 0
    plan = o["plan, finance fails"]
    assert plan.correct == 6
    assert plan.extra["failure_reported"] is True
    assert plan.extra["finance_answered"] == 0, "nothing invented for the failed branch"
    assert o["lead + 4 specialists, finance fails"].extra["failure_reported"] is True


async def test_interrupted_delegations_never_duplicate(tmp_path: Path) -> None:
    retry_safe, real = await harness.interrupted(tmp_path)
    for outcome in (retry_safe, real):
        assert outcome.phase == "completed"
        assert outcome.extra["children"] == 4, "re-executed delegations attached to their runs"
        assert outcome.extra["interrupted_attempts"] == 4
    assert retry_safe.success
    assert real.extra["failed_children"] == 4, "ambiguous outcomes fail, they are not repeated"
    assert real.correct == 0


async def test_permissions_narrow_and_nothing_leaks(tmp_path: Path) -> None:
    admin, olive = await harness.permission_boundary(tmp_path)
    assert admin.success
    assert olive.correct == 4
    assert olive.extra["leaks"] == 0
    assert olive.extra["narrowed"] == 2


async def test_budget_exhaustion_is_bounded_and_partial(tmp_path: Path) -> None:
    breadth = _by(await harness.independent_breadth(tmp_path / "breadth"))
    [outcome] = await harness.budget_exhaustion(
        tmp_path / "budget", breadth["lead + 4 specialists"].tokens
    )
    budget = outcome.extra["budget"]
    assert isinstance(budget, int)
    assert outcome.phase == "completed", "the coordinator keeps a reserve to report"
    stopped = outcome.extra["stopped"]
    assert isinstance(stopped, int)
    assert stopped >= 1
    assert outcome.correct < 8
    assert outcome.tokens <= budget * 1.25, "overshoot: at most the coordinator's final call"


async def test_runaway_delegation_is_bounded(tmp_path: Path) -> None:
    chain, fanout = await harness.runaway(tmp_path)
    assert (chain.runs, chain.extra["depth"]) == (4, 3)
    assert fanout.runs == 24
    assert fanout.extra["depth"] == 3
    refused = fanout.extra["refused"]
    assert isinstance(refused, int)
    assert refused >= 1, "delegations beyond max_runs are refused (how many depends on timing)"
