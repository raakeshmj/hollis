"""Tasks, conditions, grading and tables for the specialist evaluation.

Everything runs on the real platform: runner, tools, workspace policy, approvals, overlays,
templates, output contracts and their checks. Only the agent is simulated (`sim.py`). Three
conditions per task, all with the same simulated agent, model budget and error draws:

- **generalist**: generic file and knowledge tools and generic instructions; no contract;
- **generalist + tools**: the same, plus the specialist template's tools;
- **specialist**: an agent made from the template (its tools, procedure, overlay, contract).

Approvals are decided by the harness as a person would: changes the task asked for are
approved (engineering); changes nobody asked for are denied and counted (review).
"""

import json
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from hollis.platform.auth import Principal
from hollis.platform.config import initialize
from hollis.platform.schemas import (
    AgentInput,
    AgentOverrides,
    ApprovalDecisionInput,
    CollectionInput,
    DeploymentInput,
    FromTemplateInput,
    RunPhase,
    StartRun,
)
from hollis.platform.services import Platform
from hollis.runtime.knowledge import KnowledgeAccess
from hollis.runtime.policy import RunBudget
from hollis.runtime.store import load_run
from hollis.runtime.tools import ToolEffect
from tests.evals.specialists import corpus
from tests.evals.specialists.sim import ErrorModel, SimAgent

CONDITIONS = ("generalist", "generalist + tools", "specialist")
GENERIC_TOOLS = (
    "fs_list_dir",
    "fs_read_file",
    "fs_write_file",
    "knowledge_search",
    "knowledge_read",
)
GENERIC_INSTRUCTIONS = "You are a helpful assistant. Use the tools to do the task, then answer."
BUDGET = RunBudget(
    max_output_tokens=2048, max_model_turns=16, max_tool_calls=40, max_tool_calls_per_turn=8
)
SMALL_ROWS, LARGE_ROWS = corpus.sales_rows(40, seed=3), corpus.sales_rows(3000, seed=5)


@dataclass(frozen=True)
class Task:
    key: str
    name: str
    template: str
    input: str


TASKS = (
    Task(
        "data-small",
        "data, 40 rows",
        "data-analyst",
        corpus.DATA_QUESTION.replace("data/sales.csv", "data/small/sales.csv"),
    ),
    Task(
        "data-large",
        "data, 3,000 rows",
        "data-analyst",
        corpus.DATA_QUESTION.replace("data/sales.csv", "data/large/sales.csv"),
    ),
    Task("review", "code review", "code-reviewer", corpus.REVIEW_TASK),
    Task("research", "research", "researcher", corpus.RESEARCH_QUESTION),
    Task("engineering", "bug fix", "software-engineer", corpus.ENGINEERING_TASK),
)


@dataclass
class Result:
    task: str
    condition: str
    p: float
    r: float
    seed: int
    correct: bool
    delivered: bool
    """A result was presented as usable (not abstained, rejected or admitted as failed)."""
    claims: int = 0
    valid: int = 0
    """Delivered claims whose reference is right (value, line, citation, verification)."""
    detectable: int = 0
    """Injected errors a contract can detect, present in the first result."""
    caught: int = 0
    tokens: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    wall_ms: float = 0.0
    approvals: int = 0
    denied_by_overlay: int = 0
    tools_offered: int = 0
    mutating_offered: int = 0
    notes: list[str] = field(default_factory=list[str])

    @property
    def silent_error(self) -> bool:
        return self.delivered and not self.correct


class Bench:
    def __init__(
        self,
        platform: Platform,
        admin: Principal,
        sim: SimAgent,
        *,
        model: str = "sim",
        instructions: str = GENERIC_INSTRUCTIONS,
    ) -> None:
        self.platform = platform
        self.admin = admin
        self.sim = sim
        self.model = model
        self.instructions = instructions

    @classmethod
    async def open(
        cls,
        root: Path,
        *,
        deployment: dict[str, Any] | None = None,
        instructions: str = GENERIC_INSTRUCTIONS,
    ) -> "Bench":
        """A workspace with every task's files. `deployment`: a real model for every agent
        (the live evaluation) instead of the simulated one."""
        initialize(root, "specialist-eval")
        config = root / "hollis.toml"
        config.write_text(
            config.read_text() + "\n[[tools.commands]]\n"
            'name = "run_tests"\n'
            'description = "Run the unit tests."\n'
            f'argv = ["{sys.executable}", "-m", "unittest", "discover", "-s", "tests", "-t", "."]\n'
            'effect = "read_only"\n'
            "timeout_s = 60\n"
        )
        corpus.write_repo(root)
        for name, rows in (("small", SMALL_ROWS), ("large", LARGE_ROWS)):
            (root / "data" / name).mkdir(parents=True, exist_ok=True)
            (root / "data" / name / "sales.csv").write_text(corpus.sales_csv(rows))
        platform = await Platform.open(root)
        admin = await platform.local_admin()
        sim = SimAgent()
        platform.ws.register_provider(sim)
        await platform.create_collection(admin, CollectionInput(name="policies"))
        for key, (filename, text) in corpus.DOCUMENTS.items():
            await platform.ingest(admin, "policies", key=key, data=text.encode(), filename=filename)
        model = "sim"
        if deployment is not None:
            await platform.upsert_deployment(admin, DeploymentInput(deployment=deployment))
            model = str(deployment["id"])
        bench = cls(platform, admin, sim, model=model, instructions=instructions)
        await bench._agents()
        return bench

    async def close(self) -> None:
        await self.platform.close()

    async def _agents(self) -> None:
        knowledge = KnowledgeAccess(collections=("policies",))
        for task in TASKS:
            detail = await self.platform.template(self.admin, task.template)
            for condition in CONDITIONS:
                name = agent_name(task, condition)
                if condition == "specialist":
                    await self.platform.create_agent_from_template(
                        self.admin,
                        FromTemplateInput(
                            template=task.template,
                            name=name,
                            model=self.model,
                            overrides=AgentOverrides(knowledge=knowledge, budget=BUDGET),
                        ),
                    )
                    continue
                tools = GENERIC_TOOLS
                if condition == "generalist + tools":
                    tools = tuple(dict.fromkeys([*GENERIC_TOOLS, *detail.tools]))
                await self.platform.create_agent(
                    self.admin,
                    AgentInput(
                        name=name,
                        model=self.model,
                        instructions=self.instructions,
                        tools=tools,
                        knowledge=knowledge,
                        budget=BUDGET,
                    ),
                )

    async def run(self, task: Task, condition: str, errors: ErrorModel) -> Result:
        self.sim.errors = errors
        root = self.platform.ws.root
        if task.key == "engineering":
            corpus.reset_engineering(root)
        p = self.platform
        started = time.perf_counter()
        run = await p.start_run(
            self.admin, StartRun(input=task.input, agent=agent_name(task, condition))
        )
        approvals = 0
        summary = await p.wait_run(self.admin, run.run_id, timeout_s=60)
        while summary.phase is RunPhase.WAITING_APPROVAL:
            for approval in await p.approvals(self.admin):
                if approval.run_id != run.run_id:
                    continue
                approvals += 1
                wanted = task.key == "engineering"
                await p.decide(
                    self.admin,
                    run.run_id,
                    approval.invocation_id,
                    ApprovalDecisionInput(
                        approved=wanted, reason=None if wanted else "not asked for"
                    ),
                )
            summary = await p.wait_run(self.admin, run.run_id, timeout_s=60)
        wall_ms = (time.perf_counter() - started) * 1000
        result = await self._grade(task, condition, errors, run.run_id)
        result.wall_ms, result.approvals = wall_ms, approvals
        return result

    async def _grade(self, task: Task, condition: str, errors: ErrorModel, run_id: UUID) -> Result:
        detail = await self.platform.run(self.admin, run_id)
        state = await load_run(self.platform.ws.runs, run_id)
        refs = [state.tool(t) for t in detail.tools]
        result = Result(
            task=task.name,
            condition=condition,
            p=errors.p,
            r=errors.r,
            seed=errors.seed,
            correct=False,
            delivered=False,
            tokens=detail.summary.usage.input_tokens + detail.summary.usage.output_tokens,
            model_calls=len(detail.turns),
            tool_calls=sum(len(t.tool_calls) for t in detail.turns),
            tools_offered=len([t for t in detail.tools if not t.startswith("submit_")]),
            mutating_offered=sum(
                1 for ref in refs if ref is not None and ref.effect is not ToolEffect.READ_ONLY
            ),
            denied_by_overlay=sum(
                1
                for turn in detail.turns
                for call in turn.tool_calls
                if call.decision == "deny" and (call.rule or "").startswith("agent:")
            ),
        )
        submissions = [
            call
            for turn in detail.turns
            for call in turn.tool_calls
            if call.tool.startswith("submit_")
        ]
        data: dict[str, Any] | None
        if condition == "specialist":
            output = detail.output
            data = (
                dict(output.data)
                if output and output.status == "accepted" and output.data
                else None
            )
            first = submissions[0].arguments if submissions else None
            result.caught = int(bool(submissions) and submissions[0].status != "succeeded")
        else:
            data = _answer_json(detail.answer or "")
            first = data
        if data is not None and "abstained" in data:
            data = None
        tests = _tests_ran(detail)
        result.detectable = _detectable(task, errors, first)
        if task.key == "engineering":
            # Only a false claim is an error: "passed" without a passing run after the change.
            claims_pass = isinstance(first, dict) and _claims_pass(first)
            result.detectable = int(claims_pass and not (tests and tests[-1][1]))
            _grade_engineering(result, data, root=self.platform.ws.root, state_tests=tests)
        elif data is not None:
            result.delivered = True
            if task.key.startswith("data"):
                _grade_data(result, data, SMALL_ROWS if task.key == "data-small" else LARGE_ROWS)
            elif task.key == "review":
                _grade_review(result, data)
            else:
                _grade_research(result, data, state.evidence())
        return result


def agent_name(task: Task, condition: str) -> str:
    suffix = {"generalist": "g", "generalist + tools": "gt", "specialist": "s"}[condition]
    return f"{task.key}-{suffix}"


def _answer_json(text: str) -> dict[str, Any] | None:
    match = re.search(r"```json\n(.*?)\n```", text, re.S)
    return json.loads(match.group(1)) if match else None


def _detectable(task: Task, errors: ErrorModel, first: object) -> int:
    """Injected errors of the kinds a contract checks, in the first result."""
    if not isinstance(first, dict):
        return 0
    findings = cast(list[dict[str, Any]], cast(dict[str, Any], first).get("findings", []))
    if task.key.startswith("data"):  # the leading region is not checkable: it is reasoning
        return sum(errors.hit("data", "value", i) for i in range(len(findings) - 1))
    if task.key == "review":
        return int(any(f.get("file") == "shop/payments.py" for f in findings))
    if task.key == "research":
        return sum(errors.hit("research", "cite", i) for i in range(len(findings)))
    return 0


def _claims_pass(result: dict[str, Any]) -> bool:
    verifications = cast(list[dict[str, Any]], result.get("verifications", []))
    passed = any(v.get("outcome") == "passed" for v in verifications)
    return passed or str(result.get("summary", "")).startswith("Fixed")


def _grade_data(
    result: Result, data: dict[str, Any], rows: list[tuple[str, str, str, float]]
) -> None:
    truth = corpus.q3_totals(rows)
    leader = max(truth, key=lambda region: truth[region])
    findings = data.get("findings", [])
    result.claims = len(findings)
    for finding in findings:
        statement, value = str(finding.get("statement", "")), finding.get("value")
        region = statement.split(" ")[0]
        number = isinstance(value, int | float) and region in truth
        if (number and abs(value - truth[region]) < 0.01) or (
            statement == "leader" and value == leader
        ):
            result.valid += 1
    result.correct = result.claims == len(truth) + 1 and result.valid == result.claims


def _grade_review(result: Result, data: dict[str, Any]) -> None:
    truth = {(d.file, d.line()) for d in corpus.DEFECTS}
    found = [(str(f.get("file")), int(f.get("line", 0))) for f in data.get("findings", [])]
    result.claims = len(found)
    result.valid = sum(1 for item in found if item in truth)
    result.correct = set(found) == truth and len(found) == len(truth)


def _grade_research(result: Result, data: dict[str, Any], evidence: dict[str, str]) -> None:
    findings = data.get("findings", [])
    result.claims = len(findings)
    for fact, finding in zip(corpus.FACTS, findings, strict=False):
        statement = str(finding.get("statement", "")).lower()
        cited = [evidence.get(c, "") for c in finding.get("citations", [])]
        if fact.value in statement and any(fact.value in text.lower() for text in cited):
            result.valid += 1
    kept = len(data.get("contradictions", [])) > 0
    result.notes.append("contradiction kept" if kept else "contradiction lost")
    result.correct = result.valid == len(corpus.FACTS) == result.claims


def _tests_ran(detail: Any) -> list[tuple[str, bool]]:
    return [
        (call.invocation_id, call.status == "succeeded")
        for turn in detail.turns
        for call in turn.tool_calls
        if call.tool == "run_tests"
    ]


def _grade_engineering(
    result: Result, data: dict[str, Any] | None, *, root: Path, state_tests: list[tuple[str, bool]]
) -> None:
    namespace: dict[str, Any] = {}
    exec((root / "calc" / "stats.py").read_text(), namespace)  # noqa: S102  # the evaluated code
    code_ok = namespace["mean"]([2, 4, 6]) == 4
    if data is None:  # rejected or no result: the failure is visible
        result.delivered = False
        result.correct = False
        return
    claimed = _claims_pass(data)
    verified = bool(state_tests) and state_tests[-1][1]
    result.claims = 1
    result.valid = int(claimed == (code_ok and verified) or (not claimed and not verified))
    result.delivered = claimed or code_ok
    result.correct = code_ok
    if not claimed:
        result.notes.append("unverified" if not state_tests else "reported failing")


async def evaluate(
    root: Path,
    *,
    ps: tuple[float, ...] = (0.0, 0.1, 0.3),
    rs: tuple[float, ...] = (0.0, 0.5, 1.0),
    seeds: int = 30,
) -> list[Result]:
    bench = await Bench.open(root)
    results: list[Result] = []
    try:
        for task in TASKS:
            for p in ps:
                for r in rs if p > 0 else (1.0,):
                    for seed in range(seeds):
                        for condition in CONDITIONS:
                            results.append(await bench.run(task, condition, ErrorModel(p, r, seed)))
    finally:
        await bench.close()
    return results


# --- tables --------------------------------------------------------------------------------------


def _pct(part: float, whole: float) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "-"


def summary(results: list[Result]) -> dict[tuple[str, str, float, float], dict[str, float]]:
    groups: dict[tuple[str, str, float, float], list[Result]] = {}
    for r in results:
        groups.setdefault((r.task, r.condition, r.p, r.r), []).append(r)
    table: dict[tuple[str, str, float, float], dict[str, float]] = {}
    for key, items in groups.items():
        n = len(items)
        detectable = sum(1 for i in items if i.detectable)
        table[key] = {
            "n": n,
            "correct": sum(i.correct for i in items) / n,
            "silent": sum(i.silent_error for i in items) / n,
            "flagged": sum(not i.delivered for i in items) / n,
            "valid": sum(i.valid for i in items) / max(sum(i.claims for i in items), 1),
            "caught": sum(i.caught for i in items if i.detectable) / detectable
            if detectable
            else -1,
            "erroneous": detectable,
            "tokens": statistics.mean(i.tokens for i in items),
            "model_calls": statistics.mean(i.model_calls for i in items),
            "tool_calls": statistics.mean(i.tool_calls for i in items),
            "wall_ms": statistics.median(i.wall_ms for i in items),
            "approvals": statistics.mean(i.approvals for i in items),
            "denied": statistics.mean(i.denied_by_overlay for i in items),
            "tools": statistics.mean(i.tools_offered for i in items),
            "mutating": statistics.mean(i.mutating_offered for i in items),
            "contradiction": sum("contradiction kept" in i.notes for i in items) / n,
        }
    return table


MAIN = (0.3, 0.5)
"""The error model of the main table: 30% per error kind, half of flagged errors fixed."""


def markdown(results: list[Result]) -> str:
    table = summary(results)
    tasks = list(dict.fromkeys(r.task for r in results))
    n = next(iter(table.values()))["n"]
    lines = [
        "### Cost and scope, with no injected errors (p = 0)",
        "",
        "| Task | Condition | Correct | Tokens | Model calls | Tool calls | Tools offered "
        "| Of which can change things | Median wall time |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for task in tasks:
        for condition in CONDITIONS:
            s = table.get((task, condition, 0.0, 1.0))
            if s is not None:
                lines.append(
                    f"| {task} | {condition} | {_pct(s['correct'], 1)} | {s['tokens']:.0f} "
                    f"| {s['model_calls']:.1f} | {s['tool_calls']:.1f} | {s['tools']:.0f} "
                    f"| {s['mutating']:.0f} | {s['wall_ms']:.0f} ms |"
                )
    p, r = MAIN
    if any((t, c, p, r) in table for t in tasks for c in CONDITIONS):
        lines += [
            "",
            f"### Injected errors: p = {p} per error kind, fix rate after a rejection r = {r}",
            "",
            "| Task | Condition | Correct | Silent errors | Visible failures | Valid references "
            "| Detectable errors caught | Approvals asked |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for task in tasks:
            for condition in CONDITIONS:
                s = table.get((task, condition, p, r))
                if s is None:
                    continue
                caught = "-" if s["caught"] < 0 else _pct(s["caught"], 1)
                lines.append(
                    f"| {task} | {condition} | {_pct(s['correct'], 1)} | {_pct(s['silent'], 1)} "
                    f"| {_pct(s['flagged'], 1)} | {_pct(s['valid'], 1)} | {caught} "
                    f"| {s['approvals']:.2f} |"
                )
    grid = sorted({(x.p, x.r) for x in results if x.p > 0})
    if grid:
        header = " | ".join(f"p {gp}, r {gr}" for gp, gr in grid)
        lines += [
            "",
            "### Sensitivity: correct / silent errors across error models",
            "",
            f"| Task | Condition | {header} |",
            "|---|---|" + "---|" * len(grid),
        ]
        for task in tasks:
            for condition in CONDITIONS:
                cells: list[str] = []
                for gp, gr in grid:
                    s = table.get((task, condition, gp, gr))
                    cells.append(
                        "-" if s is None else f"{_pct(s['correct'], 1)} / {_pct(s['silent'], 1)}"
                    )
                lines.append(f"| {task} | {condition} | " + " | ".join(cells) + " |")
    lines += ["", f"{n:.0f} seeded instances per row and cell."]
    return "\n".join(lines)
