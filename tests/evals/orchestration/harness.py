"""Scenarios, conditions and metrics for the orchestration evaluation.

Each (scenario, condition) runs in a fresh workspace on the real platform: the same runner,
delegate tools, policy, limits, coordinator and views as production. Only the agents' "models"
are simulated (`sim.py`). Metrics are computed from the platform's own views and event logs.
"""

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from hollis.platform.auth import Principal
from hollis.platform.config import initialize
from hollis.platform.schemas import (
    AgentInput,
    CollectionInput,
    OrchestrationView,
    Plan,
    PlanStep,
    RunDetail,
    StartRun,
)
from hollis.platform.services import Platform
from hollis.runtime.context import ContextPolicy
from hollis.runtime.delegation import DelegationLimits
from hollis.runtime.events import ModelCallFailed, ModelCallStarted
from hollis.runtime.grounding import Support
from hollis.runtime.knowledge import KnowledgeAccess
from hollis.runtime.model import FailureKind
from hollis.runtime.policy import RunBudget
from tests.evals.orchestration import corpus
from tests.evals.orchestration.corpus import BREADTH, DESCRIPTIONS, Question
from tests.evals.orchestration.sim import SimAgent
from tests.support.platform import add_principal

DOMAINS = ("ops", "finance", "legal", "product")
NO_START_RETRIEVAL = ContextPolicy(knowledge_limit=0, memory_limit=0)


@dataclass
class Outcome:
    scenario: str
    condition: str
    phase: str
    correct: int
    total: int
    wrong: int = 0
    supported: int = 0
    """Correct facts stated in a claim whose citation the grounding check verified."""
    claims: int = 0
    fabricated: int = 0
    tokens: int = 0
    coordination: int = 0
    calls: int = 0
    runs: int = 1
    wall_s: float = 0.0
    notes: list[str] = field(default_factory=list[str])
    answer: str = ""
    extra: dict[str, object] = field(default_factory=dict[str, object])

    @property
    def success(self) -> bool:
        return self.correct == self.total and self.wrong == 0


class Bench:
    """One workspace: corpora ingested, simulated deployments registered."""

    def __init__(
        self, platform: Platform, admin: Principal, sims: list[SimAgent] | None = None
    ) -> None:
        self.platform = platform
        self.admin = admin
        self.sims = {sim.deployment: sim for sim in sims or []}

    @classmethod
    async def open(cls, root: Path, sims: list[SimAgent], collections: dict[str, str]) -> "Bench":
        initialize(root, "eval")
        platform = await Platform.open(root)
        admin = await platform.local_admin()
        for sim in sims:
            platform.ws.register_provider(sim)
        for name, text in collections.items():
            await platform.create_collection(admin, CollectionInput(name=name))
            await platform.ingest(admin, name, key=f"{name}.md", data=text.encode())
        return cls(platform, admin, sims)

    async def agent(
        self,
        name: str,
        model: str,
        *,
        collections: tuple[str, ...] = (),
        description: str = "",
        delegates: tuple[str, ...] = (),
        turns: int = 8,
        delegation: DelegationLimits | None = None,
    ) -> None:
        await self.platform.create_agent(
            self.admin,
            AgentInput(
                name=name,
                model=model,
                description=description,
                tools=("knowledge_search",) if collections else (),
                knowledge=KnowledgeAccess(collections=collections) if collections else None,
                delegates=delegates,
                delegation=delegation,
                context=NO_START_RETRIEVAL,
                budget=RunBudget(max_output_tokens=1024, max_model_turns=turns),
            ),
        )

    async def run(
        self,
        scenario: str,
        condition: str,
        task: str,
        questions: list[Question],
        *,
        agent: str | None = None,
        plan: Plan | None = None,
        principal: Principal | None = None,
        timeout_s: float = 120,
    ) -> Outcome:
        caller = principal or self.admin
        started = time.monotonic()
        summary = await self.platform.start_run(
            caller, StartRun(input=task, agent=agent, plan=plan)
        )
        summary = await self.platform.wait_run(caller, summary.run_id, timeout_s=timeout_s)
        wall = time.monotonic() - started
        return await self.measure(scenario, condition, summary.run_id, questions, wall, caller)

    async def measure(
        self,
        scenario: str,
        condition: str,
        run_id: UUID,
        questions: list[Question],
        wall: float,
        caller: Principal | None = None,
    ) -> Outcome:
        caller = caller or self.admin
        detail = await self.platform.run(caller, run_id)
        tree = await self.platform.run_tree(caller, run_id)
        answer = detail.answer or ""
        calls = 0
        for node in tree.nodes:
            for event in await self.platform.ws.runs.read(node.run.run_id):
                calls += isinstance(event.payload, ModelCallStarted)
        grounding = detail.grounding
        outcome = Outcome(
            scenario=scenario,
            condition=condition,
            phase=detail.summary.phase.value,
            correct=sum(1 for q in questions if q.fact.casefold() in answer.casefold()),
            total=len(questions),
            wrong=sum(1 for q in questions if q.wrong and q.wrong.casefold() in answer.casefold()),
            supported=sum(
                1
                for q in questions
                if grounding is not None
                and any(
                    c.support is Support.SUPPORTED and q.fact.casefold() in c.text.casefold()
                    for c in grounding.claims
                )
            ),
            claims=len(grounding.claims) if grounding else 0,
            fabricated=len(grounding.fabricated) if grounding else 0,
            tokens=tree.totals.input_tokens + tree.totals.output_tokens,
            coordination=tree.totals.coordinator_tokens if tree.totals.runs > 1 else 0,
            calls=calls,
            runs=tree.totals.runs,
            wall_s=wall,
            answer=answer,
        )
        if detail.failure is not None:
            outcome.notes.append(f"failed: {detail.failure.reason}")
        outcome.extra["tree"] = tree
        outcome.extra["detail"] = detail
        return outcome

    async def close(self) -> None:
        await self.platform.close()


def breadth_task(questions: list[Question]) -> str:
    return "Answer each question:\n" + "\n".join(f"- {q.text}" for q in questions)


def specialists_plan(questions: list[Question], agents: dict[str, str]) -> Plan:
    by_domain: dict[str, list[Question]] = {}
    for q in questions:
        by_domain.setdefault(q.collection, []).append(q)
    return Plan(
        steps=tuple(
            PlanStep(
                id=domain,
                agent=agents[domain],
                objective="\n".join(f"- {q.text}" for q in qs),
            )
            for domain, qs in by_domain.items()
        )
    )


async def _breadth_bench(
    root: Path,
    *,
    failing: str | None = None,
    hold: asyncio.Event | None = None,
    deduplicates: bool = True,
) -> Bench:
    sims = [
        SimAgent("sim-single"),
        SimAgent("sim-single-parallel", parallel=True),
        SimAgent("sim-lead", role="lead"),
        *(
            SimAgent(f"sim-{d}", fail=d == failing, hold=hold, deduplicates=deduplicates)
            for d in DOMAINS
        ),
    ]
    bench = await Bench.open(root, sims, {d: corpus.document(d) for d in DOMAINS})
    for domain in DOMAINS:
        await bench.agent(
            f"{domain}-specialist",
            f"sim-{domain}",
            collections=(domain,),
            description=DESCRIPTIONS[domain],
        )
    await bench.agent("generalist", "sim-single", collections=DOMAINS)
    await bench.agent("generalist-16", "sim-single", collections=DOMAINS, turns=16)
    await bench.agent("generalist-parallel", "sim-single-parallel", collections=DOMAINS)
    await bench.agent(
        "lead",
        "sim-lead",
        collections=DOMAINS,
        delegates=tuple(f"{d}-specialist" for d in DOMAINS),
    )
    return bench


SPECIALISTS = {d: f"{d}-specialist" for d in DOMAINS}


async def independent_breadth(root: Path) -> list[Outcome]:
    bench = await _breadth_bench(root)
    task = breadth_task(BREADTH)
    try:
        return [
            await bench.run(
                "independent", "single agent, 8 turns", task, BREADTH, agent="generalist"
            ),
            await bench.run(
                "independent", "single agent, 16 turns", task, BREADTH, agent="generalist-16"
            ),
            await bench.run(
                "independent",
                "single agent, parallel tool calls",
                task,
                BREADTH,
                agent="generalist-parallel",
            ),
            await bench.run("independent", "lead + 4 specialists", task, BREADTH, agent="lead"),
            await bench.run(
                "independent",
                "plan: 4 specialists",
                task,
                BREADTH,
                plan=specialists_plan(BREADTH, SPECIALISTS),
            ),
        ]
    finally:
        await bench.close()


async def dependent_pipeline(root: Path) -> list[Outcome]:
    sims = [SimAgent("sim-single"), SimAgent("sim-ownership")]
    bench = await Bench.open(root, sims, {"ownership": corpus.document("ownership")})
    await bench.agent("generalist", "sim-single", collections=("ownership",))
    await bench.agent("owner-finder", "sim-ownership", collections=("ownership",))
    q1, q2 = corpus.DEPENDENT
    task = breadth_task(corpus.DEPENDENT)
    chained = Plan(
        steps=(
            PlanStep(id="owner", agent="owner-finder", objective=f"- {q1.text}"),
            PlanStep(
                id="oncall", agent="owner-finder", objective=f"- {q2.text}", depends_on=("owner",)
            ),
        )
    )
    naive = Plan(
        steps=(
            PlanStep(id="owner", agent="owner-finder", objective=f"- {q1.text}"),
            PlanStep(id="oncall", agent="owner-finder", objective=f"- {q2.text}"),
        )
    )
    try:
        return [
            await bench.run(
                "dependent", "single agent", task, corpus.DEPENDENT, agent="generalist"
            ),
            await bench.run(
                "dependent", "plan: chained, results passed", task, corpus.DEPENDENT, plan=chained
            ),
            await bench.run(
                "dependent", "plan: naive parallel split", task, corpus.DEPENDENT, plan=naive
            ),
        ]
    finally:
        await bench.close()


async def conflicting_sources(root: Path) -> list[Outcome]:
    sims = [
        SimAgent("sim-single"),
        SimAgent("sim-a"),
        SimAgent("sim-b"),
        SimAgent("sim-review", role="reviewer"),
    ]
    documents = {
        "handbook-2024": f"# Handbook 2024\n\n## Refunds\n\n{corpus.REFUND_OLD}\n",
        "policy-2026": f"# Policy 2026\n\n## Refunds\n\n{corpus.REFUND_NEW}\n",
    }
    bench = await Bench.open(root, sims, documents)
    await bench.agent("generalist", "sim-single", collections=tuple(documents))
    await bench.agent("handbook-reader", "sim-a", collections=("handbook-2024",))
    await bench.agent("policy-reader", "sim-b", collections=("policy-2026",))
    await bench.agent("reviewer", "sim-review")
    question = corpus.CONFLICT
    objective = f"- {question.text}"
    plan = Plan(
        steps=(
            PlanStep(id="handbook", agent="handbook-reader", objective=objective),
            PlanStep(id="policy", agent="policy-reader", objective=objective),
            PlanStep(
                id="review",
                agent="reviewer",
                objective="Reconcile the answers.",
                depends_on=("handbook", "policy"),
            ),
        )
    )
    try:
        single = await bench.run(
            "conflicting", "single agent", objective, [question], agent="generalist"
        )
        multi = await bench.run(
            "conflicting", "plan: 2 readers + reviewer", objective, [question], plan=plan
        )
    finally:
        await bench.close()
    for outcome in (single, multi):
        tree = outcome.extra["tree"]
        assert isinstance(tree, OrchestrationView)
        surfaced = bool(tree.disagreements) or "disagree" in outcome.answer
        outcome.extra["surfaced"] = surfaced
        conclusion = outcome.answer.split("## review", 1)[-1].split("\nDisagreements", 1)[0]
        outcome.correct = int(question.fact in conclusion)
        outcome.wrong = int(question.wrong is not None and question.wrong in conclusion)
        outcome.notes.append("disagreement surfaced" if surfaced else "disagreement not surfaced")
    return [single, multi]


async def agent_failure(root: Path) -> list[Outcome]:
    bench = await _breadth_bench(root, failing="finance")
    task = breadth_task(BREADTH)
    broken = SimAgent("sim-broken", fail=True)
    bench.platform.ws.register_provider(broken)
    await bench.agent("generalist-broken", "sim-broken", collections=DOMAINS, turns=16)
    try:
        results = [
            await bench.run(
                "agent failure",
                "single agent, its model fails",
                task,
                BREADTH,
                agent="generalist-broken",
            ),
            await bench.run(
                "agent failure", "lead + 4 specialists, finance fails", task, BREADTH, agent="lead"
            ),
            await bench.run(
                "agent failure",
                "plan, finance fails",
                task,
                BREADTH,
                plan=specialists_plan(BREADTH, SPECIALISTS),
            ),
        ]
    finally:
        await bench.close()
    finance = [q for q in BREADTH if q.collection == "finance"]
    for outcome in results:
        reported = "finance" in outcome.answer and (
            "did not" in outcome.answer or "Not answered" in outcome.answer
        )
        outcome.extra["failure_reported"] = reported
        outcome.extra["finance_answered"] = sum(1 for q in finance if q.fact in outcome.answer)
        if outcome.runs > 1:
            outcome.notes.append("failure reported" if reported else "failure not reported")
    return results


async def interrupted(root: Path) -> list[Outcome]:
    return [
        await _interrupted(root / "dedup", deduplicates=True),
        await _interrupted(root / "real", deduplicates=False),
    ]


async def _interrupted(root: Path, *, deduplicates: bool) -> Outcome:
    """Stop the process while all four specialists are mid-call, reopen, recover."""
    hold = asyncio.Event()
    bench = await _breadth_bench(root, hold=hold, deduplicates=deduplicates)
    task = breadth_task(BREADTH)
    plan = specialists_plan(BREADTH, SPECIALISTS)
    started = time.monotonic()
    summary = await bench.platform.start_run(bench.admin, StartRun(input=task, plan=plan))
    specialists = [bench.sims[f"sim-{d}"] for d in DOMAINS]
    await asyncio.wait_for(asyncio.gather(*(s.entered.wait() for s in specialists)), 30)
    await bench.close()  # every specialist is mid-call
    reopened = await _reopen(root, deduplicates=deduplicates)
    kind = "retry-safe deployment" if deduplicates else "non-deduplicating deployment"
    try:
        resumed = await reopened.platform.ws.recover()
        await reopened.platform.wait_run(reopened.admin, summary.run_id, timeout_s=120)
        outcome = await reopened.measure(
            "interrupted",
            f"plan, stopped mid-delegation ({kind})",
            summary.run_id,
            BREADTH,
            time.monotonic() - started,
        )
        tree = outcome.extra["tree"]
        assert isinstance(tree, OrchestrationView)
        interrupted_attempts = 0
        for node in tree.nodes:
            for event in await reopened.platform.ws.runs.read(node.run.run_id):
                payload = event.payload
                interrupted_attempts += isinstance(payload, ModelCallFailed) and (
                    payload.failure.kind is FailureKind.INTERRUPTED
                )
        failed = sum(
            1 for n in tree.nodes if n.report is not None and n.report.outcome != "completed"
        )
        outcome.extra |= {
            "resumed": len(resumed),
            "children": tree.totals.runs - 1,
            "interrupted_attempts": interrupted_attempts,
            "failed_children": failed,
        }
        outcome.notes.append(
            f"{len(resumed)} runs resumed, {tree.totals.runs - 1} children (4 planned), "
            f"{interrupted_attempts} interrupted calls, {failed} children failed"
        )
        return outcome
    finally:
        await reopened.close()


async def _reopen(root: Path, *, deduplicates: bool = True) -> Bench:
    platform = await Platform.open(root)
    sims = [
        SimAgent("sim-single"),
        SimAgent("sim-single-parallel", parallel=True),
        SimAgent("sim-lead", role="lead"),
        *(SimAgent(f"sim-{d}", deduplicates=deduplicates) for d in DOMAINS),
    ]
    for sim in sims:
        platform.ws.register_provider(sim)
    return Bench(platform, await platform.local_admin(), sims)


async def permission_boundary(root: Path) -> list[Outcome]:
    bench = await _breadth_bench(root)
    olive = await add_principal(
        bench.platform,
        bench.admin,
        "olive",
        knowledge=KnowledgeAccess(collections=("ops", "product")),
    )
    task = breadth_task(BREADTH)
    plan = specialists_plan(BREADTH, SPECIALISTS)
    try:
        admin = await bench.run(
            "permissions", "plan as admin (all collections)", task, BREADTH, plan=plan
        )
        limited = await bench.run(
            "permissions",
            "plan as olive (ops, product only)",
            task,
            BREADTH,
            plan=plan,
            principal=olive,
        )
        hidden = [corpus.FACTS[q.fact] for q in BREADTH if q.collection in ("finance", "legal")]
        tree = limited.extra["tree"]
        assert isinstance(tree, OrchestrationView)
        leaks = 0
        for node in tree.nodes:
            for event in await bench.platform.ws.runs.read(node.run.run_id):
                raw = event.model_dump_json()
                leaks += sum(1 for fact in hidden if fact in raw)
        narrowed = sum(1 for n in tree.nodes if n.narrowed)
        limited.extra |= {"leaks": leaks, "narrowed": narrowed}
        limited.notes.append(f"{narrowed} runs narrowed, {leaks} leaked passages")
        return [admin, limited]
    finally:
        await bench.close()


async def budget_exhaustion(root: Path, unconstrained_tokens: int) -> list[Outcome]:
    budget = unconstrained_tokens // 2
    bench = await _breadth_bench(root)
    await bench.agent(
        "frugal-lead",
        "sim-lead",
        collections=DOMAINS,
        delegates=tuple(f"{d}-specialist" for d in DOMAINS),
        delegation=DelegationLimits(token_budget=budget),
    )
    try:
        outcome = await bench.run(
            "budget",
            f"lead + 4 specialists, budget {budget}",
            breadth_task(BREADTH),
            BREADTH,
            agent="frugal-lead",
        )
    finally:
        await bench.close()
    tree = outcome.extra["tree"]
    assert isinstance(tree, OrchestrationView)
    stopped = sum(
        1 for n in tree.nodes if n.report is not None and n.report.outcome == "budget_exhausted"
    )
    outcome.extra |= {"budget": budget, "stopped": stopped}
    outcome.notes.append(
        f"{stopped} specialists stopped by the budget; tokens {outcome.tokens} of {budget}"
    )
    return [outcome]


async def runaway(root: Path) -> list[Outcome]:
    sims = [SimAgent("sim-chain", always_delegate=1), SimAgent("sim-fanout", always_delegate=3)]
    bench = await Bench.open(root, sims, {})
    await bench.platform.patch_config(
        bench.admin, {"orchestration": {"max_depth": 3, "max_runs": 24}}
    )
    await bench.agent("chain", "sim-chain", delegates=("chain",))
    await bench.agent("fanout", "sim-fanout", delegates=("fanout",))
    results: list[Outcome] = []
    try:
        for name in ("chain", "fanout"):
            outcome = await bench.run(
                "runaway", f"self-delegating {name}", "- go deeper", [], agent=name
            )
            tree = outcome.extra["tree"]
            assert isinstance(tree, OrchestrationView)
            outcome.extra |= {"depth": tree.totals.max_depth, "refused": len(tree.refusals)}
            outcome.notes.append(
                f"{tree.totals.runs} runs, depth {tree.totals.max_depth}, "
                f"{len(tree.refusals)} delegations refused"
            )
            results.append(outcome)
    finally:
        await bench.close()
    return results


Scenario = Callable[[Path], Awaitable[list[Outcome]]]


async def evaluate(directory: Path) -> list[Outcome]:
    outcomes: list[Outcome] = []
    breadth = await independent_breadth(directory / "breadth")
    outcomes += breadth
    lead = next(o for o in breadth if o.condition == "lead + 4 specialists")
    outcomes += await dependent_pipeline(directory / "dependent")
    outcomes += await conflicting_sources(directory / "conflict")
    outcomes += await agent_failure(directory / "failure")
    outcomes += await interrupted(directory / "interrupted")
    outcomes += await permission_boundary(directory / "permissions")
    outcomes += await budget_exhaustion(directory / "budget", lead.tokens)
    outcomes += await runaway(directory / "runaway")
    return outcomes


def markdown(outcomes: list[Outcome]) -> str:
    lines: list[str] = []
    for scenario in dict.fromkeys(o.scenario for o in outcomes):
        lines += [
            f"#### {scenario}",
            "",
            "| condition | outcome | correct | verified (cited and supported) | tokens | "
            "coordination | model calls | runs | wall | notes |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for o in (x for x in outcomes if x.scenario == scenario):
            correct = (
                f"{o.correct}/{o.total}" + (f", {o.wrong} wrong" if o.wrong else "")
                if o.total
                else "-"
            )
            evidence = (f"{o.supported}/{o.total}" if o.total else "-") + (
                f", {o.fabricated} fabricated" if o.fabricated else ""
            )
            lines.append(
                f"| {o.condition} | {o.phase} | {correct} | {evidence} | {o.tokens:,} | "
                f"{o.coordination:,} | {o.calls} | {o.runs} | {o.wall_s:.2f}s | "
                f"{'; '.join(o.notes)} |"
            )
        lines.append("")
    return "\n".join(lines)


async def run_all(directory: Path) -> list[Outcome]:
    with contextlib.suppress(FileExistsError):
        directory.mkdir(parents=True)
    return await evaluate(directory)


def details(outcome: Outcome) -> RunDetail:
    detail = outcome.extra["detail"]
    assert isinstance(detail, RunDetail)
    return detail
