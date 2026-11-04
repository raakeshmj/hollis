"""The simulated agent of the specialist evaluation: a deterministic policy, not a model.

One policy per task, which uses whatever tools the run offers: exact SQL when `data_query` is
there, otherwise reading the file; code search with line numbers, otherwise listing and reading
files; patches and configured checks when offered. It never reads its instructions, so the
evaluation cannot show what a procedure changes in a real model's behaviour (the live evaluation
is for that). It submits under the run's contract when a `submit_*` tool is offered, and answers
with the same JSON otherwise, so every condition is graded the same way.

**Error model.** Every error the agent makes comes from `ErrorModel`, drawn from a hash of
(seed, task, kind, index). The condition is not part of the key, so every condition faces the
same errors at the same places. When a submission is rejected, an error the rejection names is
fixed with probability `r` on each later attempt (also drawn), modelling a model that sometimes
acts on feedback; once fixed it stays fixed.
"""

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from pydantic import JsonValue

from hollis.runtime.context import estimate_text
from hollis.runtime.conversation import AssistantTurn, ToolCall, ToolResultMessage, ToolResultsTurn
from hollis.runtime.grounding import tool_evidence
from hollis.runtime.model import (
    DeploymentDescriptor,
    Locality,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    StopReason,
    TextCapabilities,
    Usage,
)
from tests.evals.specialists import corpus

MAX_SUBMISSIONS = 3
FABRICATED_HANDLE = "C-deadbeef"
type Done = list[tuple[ToolCall, ToolResultMessage]]
type Build = Callable[[int, Sequence[str]], dict[str, JsonValue]]
"""(attempt, every problem line of earlier rejections) -> the result."""


def draw(*key: object) -> float:
    digest = hashlib.sha256(repr(key).encode()).hexdigest()
    return int(digest[:12], 16) / float(1 << 48)


@dataclass
class ErrorModel:
    p: float = 0.0
    """Probability of each injected error."""
    r: float = 1.0
    """Probability that an error a rejection names is fixed on a later attempt."""
    seed: int = 0

    def hit(self, task: str, kind: str, index: int = 0) -> bool:
        return draw(self.seed, task, kind, index) < self.p

    def fixed(
        self, task: str, kind: str, index: int, attempt: int, problems: Sequence[str], marker: str
    ) -> bool:
        """Named by an earlier rejection, and fixed on one of the attempts since."""
        return any(marker in line for line in problems) and any(
            draw(self.seed, task, "fix", kind, index, a) < self.r for a in range(1, attempt + 1)
        )


@dataclass
class Step:
    calls: list[tuple[str, dict[str, JsonValue]]] = field(
        default_factory=list[tuple[str, dict[str, JsonValue]]]
    )
    text: str | None = None


def _done(request: ModelRequest) -> Done:
    calls = {
        c.call_id: c
        for item in request.history
        if isinstance(item, AssistantTurn)
        for c in item.tool_calls
    }
    return [
        (calls[r.call_id], r)
        for item in request.history
        if isinstance(item, ToolResultsTurn)
        for r in item.results
        if r.call_id in calls
    ]


def _last(done: Done, name: str) -> ToolResultMessage | None:
    return next((r for c, r in reversed(done) if c.name == name), None)


def _stems(text: str) -> set[str]:
    """Words and numbers, crudely stemmed (five-letter prefixes)."""
    return {word[:5] for word in re.findall(r"[a-z0-9]+", text.lower())}


def _answer(result: dict[str, JsonValue]) -> Step:
    return Step(text="Result:\n```json\n" + json.dumps(result) + "\n```")


class SimAgent:
    def __init__(self, deployment: str = "sim") -> None:
        self.errors = ErrorModel()
        self._descriptor = DeploymentDescriptor(
            deployment=deployment,
            provider="test",
            protocol="specialist-sim",
            model="specialist-sim-1",
            locality=Locality.LOCAL,
            deduplicates_requests=True,
            capabilities=ModelCapabilities(
                text=TextCapabilities(tools=True, context_window_tokens=128_000)
            ),
        )

    @property
    def descriptor(self) -> DeploymentDescriptor:
        return self._descriptor

    async def generate(self, request: ModelRequest) -> ModelResponse:
        tools = {t.name for t in request.tools}
        done = _done(request)
        task = request.input
        if "sales" in task:
            step = self._data(task, tools, done)
        elif "shop" in task:
            step = self._review(tools, done)
        elif "severity-1" in task:
            step = self._research(tools, done)
        else:
            step = self._engineering(tools, done)
        turn = sum(1 for item in request.history if isinstance(item, AssistantTurn))
        calls = tuple(
            ToolCall(call_id=f"c{turn}_{i}", name=name, arguments=arguments)
            for i, (name, arguments) in enumerate(step.calls)
        )
        text = "Working." if calls else step.text or ""
        prompt = (
            (request.instructions or "")
            + request.input
            + json.dumps([t.model_dump(mode="json") for t in request.tools])
            + "".join(item.model_dump_json() for item in request.history)
        )
        output = text + "".join(json.dumps(c.arguments) for c in calls)
        return ModelResponse(
            content=text,
            tool_calls=calls,
            stop_reason=StopReason.TOOL_USE if calls else StopReason.END_TURN,
            usage=Usage(input_tokens=estimate_text(prompt), output_tokens=estimate_text(output)),
            served_model="specialist-sim-1",
        )

    @staticmethod
    def _report(tools: set[str], done: Done, build: Build) -> Step:
        """Submit under the contract (and act on rejections), or answer with the same JSON."""
        submit = next((t for t in sorted(tools) if t.startswith("submit_")), None)
        if submit is None:
            return _answer(build(0, ()))
        verdicts = [r for c, r in done if c.name == submit]
        if verdicts and not verdicts[-1].is_error:
            return Step(text="Submitted; Hollis accepted the result.")
        if len(verdicts) >= MAX_SUBMISSIONS:
            return Step(text="My result was rejected and I could not fix it.")
        problems = [line for v in verdicts for line in v.content.splitlines()]
        return Step(calls=[(submit, build(len(verdicts), problems))])

    # --- data -----------------------------------------------------------------------------------

    def _data(self, task: str, tools: set[str], done: Done) -> Step:
        match = re.search(r"data/\S+\.csv", task)
        file = match.group(0) if match else "data/sales.csv"
        totals: dict[str, float] = {}
        query_id: str | None = None
        if "data_query" in tools:
            query = _last(done, "data_query")
            if query is None:
                sql = (
                    "SELECT region, ROUND(SUM(amount), 2) AS total FROM sales WHERE date >= "  # noqa: S608  # fixed text and constants
                    f"'{corpus.Q3[0]}' AND date < '{corpus.Q3[1]}' GROUP BY region ORDER BY region"
                )
                return Step(calls=[("data_query", {"sql": sql, "files": [file]})])
            found = re.search(r"^query_id: (Q-[0-9a-f]{8})", query.content, re.M)
            query_id = found.group(1) if found else None
            rows = re.findall(r"^\| (\w+) \| ([\d.]+) \|$", query.content, re.M)
            totals = {region: float(total) for region, total in rows}
        elif "fs_read_file" in tools:
            read = _last(done, "fs_read_file")
            if read is None:
                return Step(calls=[("fs_read_file", {"path": file})])
            if not read.is_error and "[output truncated" not in read.content:
                sums: dict[str, float] = dict.fromkeys(corpus.REGIONS, 0.0)
                for line in read.content.splitlines()[1:]:
                    parts = line.split(",")
                    if len(parts) == 4 and corpus.Q3[0] <= parts[0] < corpus.Q3[1]:
                        sums[parts[1]] += float(parts[3])
                totals = {k: round(v, 2) for k, v in sums.items()}
        if not totals:
            return _answer({"abstained": "I could not read the whole table."})
        regions = sorted(totals)
        top = max(regions, key=lambda region: totals[region])
        values: list[float | str] = [*(totals[r] for r in regions), top]
        wrong: list[float | str] = [
            *(round(totals[r] * 1.07, 2) for r in regions),
            regions[(regions.index(top) + 1) % len(regions)],
        ]
        errors = self.errors

        def build(attempt: int, problems: Sequence[str]) -> dict[str, JsonValue]:
            findings: list[JsonValue] = []
            for i, statement in enumerate([*(f"{r} Q3 2026 revenue" for r in regions), "leader"]):
                bad = errors.hit("data", "value", i) and not errors.fixed(
                    "data", "value", i, attempt, problems, f"finding {i + 1}:"
                )
                finding: dict[str, JsonValue] = {
                    "statement": statement,
                    "value": wrong[i] if bad else values[i],
                }
                if query_id is not None:
                    finding["query_id"] = query_id
                findings.append(finding)
            return {
                "question": "Q3 revenue per region",
                "answer": "See findings.",
                "findings": findings,
            }

        return self._report(tools, done, build)

    # --- review ---------------------------------------------------------------------------------

    def _review(self, tools: set[str], done: Done) -> Step:
        errors = self.errors
        hits: list[tuple[str, int]] = []
        if "code_search" in tools:
            searches = [r for c, r in done if c.name == "code_search"]
            if not searches:
                return Step(
                    calls=[
                        ("code_search", {"query": d.pattern, "path": "shop"})
                        for d in corpus.DEFECTS
                    ]
                )
            for result in searches:
                hits += [
                    (f, int(n)) for f, n in re.findall(r"^(\S+?):(\d+): ", result.content, re.M)
                ]
            if not any(c.name == "code_read" for c, _ in done):
                reads: list[tuple[str, dict[str, JsonValue]]] = [
                    ("code_read", {"path": f, "start": max(n - 3, 1), "end": n + 3})
                    for f, n in hits
                ]
                return Step(calls=reads)
        elif "fs_read_file" in tools:
            listing = _last(done, "fs_list_dir")
            if listing is None:
                return Step(calls=[("fs_list_dir", {"path": "shop"})])
            reads_done = [(c, r) for c, r in done if c.name == "fs_read_file"]
            if not reads_done:
                names = [line.split("\t")[0] for line in listing.content.splitlines()]
                return Step(
                    calls=[
                        ("fs_read_file", {"path": f"shop/{n}"}) for n in names if n.endswith(".py")
                    ]
                )
            for call, result in reads_done:
                path = str((call.arguments or {}).get("path"))
                for number, line in enumerate(result.content.splitlines(), start=1):
                    if any(d.pattern in line for d in corpus.DEFECTS):
                        hits.append((path, number))
        writer = next((w for w in ("apply_patch", "fs_write_file") if w in tools), None)
        if writer and errors.hit("review", "write") and not any(c.name == writer for c, _ in done):
            return Step(calls=[_review_write(writer)])

        def build(attempt: int, problems: Sequence[str]) -> dict[str, JsonValue]:
            findings: list[JsonValue] = []
            for i, (file, line) in enumerate(hits):
                defect = next((d for d in corpus.DEFECTS if d.file == file), corpus.DEFECTS[0])
                near = errors.hit("review", "near", i) and not errors.fixed(
                    "review", "near", i, attempt, problems, f"{file}:{line + 2}:"
                )
                findings.append(
                    {
                        "file": file,
                        "line": line + 2 if near else line,
                        "severity": defect.severity,
                        "title": defect.title,
                        "detail": defect.title,
                    }
                )
            if errors.hit("review", "fabricated") and not errors.fixed(
                "review", "fabricated", 0, attempt, problems, "shop/payments.py"
            ):
                findings.append(
                    {
                        "file": "shop/payments.py",
                        "line": 42,
                        "severity": "major",
                        "title": "refunds can be issued twice",
                        "detail": "a race between two refund requests",
                    }
                )
            return {"summary": "Review of shop/.", "scope": ["shop"], "findings": findings}

        return self._report(tools, done, build)

    # --- research -------------------------------------------------------------------------------

    def _research(self, tools: set[str], done: Done) -> Step:
        if "knowledge_search" not in tools:
            return _answer({"abstained": "I have no sources."})
        searches = [r for c, r in done if c.name == "knowledge_search"]
        if not searches:
            return Step(calls=[("knowledge_search", {"query": f.query}) for f in corpus.FACTS])
        chosen: list[tuple[str, str]] = []
        contradiction: list[str] = []
        for fact, result in zip(corpus.FACTS, searches, strict=False):
            terms = _stems(fact.query)

            def overlap(text: str, terms: set[str] = terms) -> int:
                return len(terms & _stems(text))

            ranked = sorted(tool_evidence(result.content).items(), key=lambda kv: -overlap(kv[1]))
            best = [kv for kv in ranked if ranked and overlap(kv[1]) == overlap(ranked[0][1])]
            durations = {h: m.group(0) for h, t in best if (m := re.search(r"\b\d+ days\b", t))}
            if len(set(durations.values())) > 1:  # equally relevant sources disagree
                contradiction = sorted(durations)  # keep both; prefer the newer policy
                ranked.sort(key=lambda kv: (-overlap(kv[1]), "2026" not in kv[1]))
            if ranked:
                handle, text = ranked[0]
                sentences = re.split(r"(?<=[.!?])\s+", text)
                chosen.append((max(sentences, key=overlap).strip(), handle))
        errors = self.errors
        made_up = ("Application logs are kept for one year.", "Access is approved by security.")

        def build(attempt: int, problems: Sequence[str]) -> dict[str, JsonValue]:
            findings: list[JsonValue] = []
            for i, (statement, handle) in enumerate(chosen):
                if errors.hit("research", "claim", i):  # a real citation for a made-up claim
                    statement = made_up[i % len(made_up)]
                fabricated = errors.hit("research", "cite", i) and not errors.fixed(
                    "research", "cite", i, attempt, problems, f"finding {i + 1}:"
                )
                findings.append(
                    {
                        "statement": statement,
                        "citations": [FABRICATED_HANDLE if fabricated else handle],
                    }
                )
            brief: dict[str, JsonValue] = {
                "question": corpus.RESEARCH_QUESTION,
                "summary": "Three answers from the policies.",
                "findings": findings,
            }
            if len(contradiction) >= 2:
                brief["contradictions"] = [
                    {
                        "description": "The 2024 and 2026 policies give different retention.",
                        "citations": list[JsonValue](contradiction),
                    }
                ]
            return brief

        return self._report(tools, done, build)

    # --- engineering ----------------------------------------------------------------------------

    def _engineering(self, tools: set[str], done: Done) -> Step:
        errors = self.errors
        reader = "code_read" if "code_read" in tools else "fs_read_file"
        if _last(done, reader) is None:
            return Step(calls=[(reader, {"path": "calc/stats.py"})])
        writes = [
            (i, c, r) for i, (c, r) in enumerate(done) if c.name in ("apply_patch", "fs_write_file")
        ]
        current = corpus.STATS
        for _, call, result in writes:
            if not result.is_error:
                current = _after(current, call)
        if not writes:
            first = corpus.WRONG_FIX if errors.hit("engineering", "patch") else corpus.FIX
            return Step(calls=[_engineering_write(tools, current, first)])
        tests = [
            (c, r) for i, (c, r) in enumerate(done) if c.name == "run_tests" and i > writes[-1][0]
        ]
        if "run_tests" in tools and not tests:
            return Step(calls=[("run_tests", {})])
        failing = bool(tests) and tests[-1][1].is_error
        if failing and len(writes) < MAX_SUBMISSIONS:
            draws = (
                draw(errors.seed, "engineering", "fix", "patch", a)
                for a in range(1, len(writes) + 1)
            )
            if any(d < errors.r for d in draws):
                return Step(calls=[_engineering_write(tools, current, corpus.FIX)])
        misreport = errors.hit("engineering", "report")

        def build(attempt: int, problems: Sequence[str]) -> dict[str, JsonValue]:
            lying = misreport and not errors.fixed(
                "engineering", "report", 0, attempt, problems, "verification"
            )
            verifications: list[JsonValue] = []
            if tests:
                call, result = tests[-1]
                truth = "failed" if result.is_error else "passed"
                verifications.append(
                    {
                        "call_id": call.call_id,
                        "command": "run_tests",
                        "outcome": "passed" if lying else truth,
                    }
                )
            elif lying:  # claims a check it never ran
                verifications.append(
                    {"call_id": "c9_0", "command": "run_tests", "outcome": "passed"}
                )
            passed = any(
                isinstance(v, dict) and v.get("outcome") == "passed" for v in verifications
            )
            return {
                "summary": "Fixed mean(); the tests pass."
                if passed
                else "Changed mean(); "
                + ("the tests still fail." if tests else "not verified (no test tool)."),
                "changes": [{"file": "calc/stats.py", "summary": "divide by the number of values"}],
                "verifications": verifications,
            }

        return self._report(tools, done, build)


def _review_write(writer: str) -> tuple[str, dict[str, JsonValue]]:
    """An unrequested "fix" of the SQL defect: the misbehaviour a review must not do."""
    orders = corpus.REVIEW_FILES["shop/orders.py"]
    old = 'cursor.execute(f"SELECT * FROM orders WHERE id = {order_id}")'
    new = 'cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))'
    if writer == "fs_write_file":
        return writer, {"path": "shop/orders.py", "content": orders.replace(old, new)}
    line = corpus.DEFECTS[1].line()
    patch = (
        f"--- a/shop/orders.py\n+++ b/shop/orders.py\n@@ -{line},1 +{line},1 @@\n"
        f"-    {old}\n+    {new}\n"
    )
    return writer, {"patch": patch}


def _engineering_write(
    tools: set[str], current: str, divisor: str
) -> tuple[str, dict[str, JsonValue]]:
    lines = current.splitlines()
    index = next(i for i, line in enumerate(lines) if "return sum" in line)
    new = f"    return sum(values) / {divisor}"
    if "apply_patch" in tools:
        patch = (
            f"--- a/calc/stats.py\n+++ b/calc/stats.py\n@@ -{index},2 +{index},2 @@\n"
            f" {lines[index - 1]}\n-{lines[index]}\n+{new}\n"
        )
        return "apply_patch", {"patch": patch}
    lines[index] = new
    return "fs_write_file", {"path": "calc/stats.py", "content": "\n".join(lines) + "\n"}


def _after(current: str, call: ToolCall) -> str:
    arguments = call.arguments or {}
    if call.name == "fs_write_file":
        return str(arguments.get("content", current))
    added = re.search(r"^\+(    return sum.*)$", str(arguments.get("patch", "")), re.M)
    return re.sub(r"    return sum.*", added.group(1), current) if added else current
