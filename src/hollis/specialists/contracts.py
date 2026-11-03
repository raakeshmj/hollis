"""Output contracts: structured results checked against the run's own log (ADR-0034).

A run whose agent has a contract gets a `submit_<contract>` tool. Its arguments are validated by
the contract's schema (malformed arguments are fed back like any invalid tool call), then
**against what the run's log shows actually happened**: a file reported as changed was changed
by this run, a verification names a command this run ran and its recorded outcome, a number
comes from a query this run executed and appears in that query's recorded result, a citation is
evidence this run received. Problems are returned to the model, which can fix them and submit
again within its budget. The accepted submission is the run's structured output.

Checks read only recorded facts (`RunFacts`), plus the filesystem for whether a cited file and
line exist. They never trust the model's own account of what it did. What they cannot check is
whether a finding is *right*: a review finding can point at a real line and still be wrong.
"""

import json
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from hollis.runtime.delegation import REPORT_KIND
from hollis.runtime.events import ToolOutcome
from hollis.runtime.grounding import CITATION
from hollis.runtime.results import render_parts
from hollis.runtime.state import RunState
from hollis.runtime.tools import JsonPart, ToolEffect, ToolError
from hollis.tools.builtin.data import query_id
from hollis.tools.builtin.filesystem import resolve_within

OUTPUT_KIND = "hollis.output"
"""The `kind` of the JSON part an accepted submission records."""
SUBMIT_PREFIX = "submit_"


def submit_tool_name(contract_id: str) -> str:
    return SUBMIT_PREFIX + contract_id.replace("-", "_")


# --- facts from the log ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """A finished tool invocation of the run, in order."""

    order: int
    invocation_id: str
    name: str
    arguments: dict[str, JsonValue]
    outcome: ToolOutcome
    text: str
    effect: ToolEffect | None
    data: tuple[JsonValue, ...] = ()
    """The result's JSON parts."""


@dataclass(frozen=True)
class RunFacts:
    calls: tuple[Call, ...]
    evidence: dict[str, str]
    root: Path | None
    """The filesystem root of the workspace's file tools, if any."""
    children: dict[str, str] = field(default_factory=dict[str, str])
    """Delegated run id -> Hollis-observed outcome, from delegation reports."""

    @classmethod
    def of(cls, state: RunState, root: Path | None) -> "RunFacts":
        calls: list[Call] = []
        children: dict[str, str] = {}
        for turn in state.turns:
            for invocation in turn.invocations:
                if invocation.outcome is None:
                    continue
                ref = state.tool(invocation.call.name)
                data = tuple(p.data for p in invocation.parts if isinstance(p, JsonPart))
                for item in data:
                    if isinstance(item, dict) and item.get("kind") == REPORT_KIND:
                        run_id, outcome = item.get("run_id"), item.get("outcome")
                        if isinstance(run_id, str) and isinstance(outcome, str):
                            children[run_id] = outcome
                calls.append(
                    Call(
                        order=len(calls),
                        invocation_id=invocation.invocation_id,
                        name=invocation.call.name,
                        arguments=invocation.arguments or {},
                        outcome=invocation.outcome,
                        text=render_parts(invocation.parts),
                        effect=ref.effect if ref is not None else None,
                        data=data,
                    )
                )
        return cls(calls=tuple(calls), evidence=state.evidence(), root=root, children=children)

    def call(self, invocation_id: str) -> Call | None:
        return next((c for c in self.calls if c.invocation_id == invocation_id), None)

    def changes(self) -> dict[str, int]:
        """File -> order of the last successful call that changed it."""
        changed: dict[str, int] = {}
        for call in self.calls:
            if call.outcome is not ToolOutcome.SUCCESS:
                continue
            for path in _changed_paths(call):
                changed[path] = call.order
        return changed


def _norm(path: str) -> str:
    text = path.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/") or "."


def _changed_paths(call: Call) -> list[str]:
    if call.name == "apply_patch":
        paths = call.arguments.get("paths")
        return [_norm(p) for p in paths if isinstance(p, str)] if isinstance(paths, list) else []
    if call.name == "fs_write_file":
        path = call.arguments.get("path")
        return [_norm(path)] if isinstance(path, str) else []
    return []


# --- contracts ------------------------------------------------------------------------------------


class _Output(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


Text = Annotated[str, Field(min_length=1, max_length=2000)]
Short = Annotated[str, Field(min_length=1, max_length=300)]
FilePath = Annotated[str, Field(min_length=1, max_length=500)]
InvocationId = Annotated[str, Field(min_length=1, max_length=200)]


@dataclass(frozen=True)
class Contract:
    id: str
    version: str
    name: str
    description: str
    schema: type[_Output]
    rules: tuple[str, ...]
    """What is checked against the log, in plain words (shown to the model and to people)."""
    check: Callable[[_Output, RunFacts], list[str]]

    @property
    def tool_name(self) -> str:
        return submit_tool_name(self.id)

    def guidance(self) -> str:
        rules = "\n".join(f"- {rule}" for rule in self.rules)
        return (
            f"## Output contract: {self.name}\n\n"
            f"When the work is done, submit your result by calling `{self.tool_name}`. Hollis "
            f"checks it against what this run actually did:\n{rules}\n"
            "If it is rejected, the problems are listed: fix them (do the missing work, or "
            "correct the report) and submit again. Then end with a short summary."
        )


# change-report ------------------------------------------------------------------------------------


class Change(_Output):
    file: FilePath
    summary: Text


class Verification(_Output):
    invocation_id: InvocationId
    """The tool call that ran the check."""
    command: Short
    """Its tool name."""
    outcome: Literal["passed", "failed"]
    notes: str = Field(default="", max_length=1000)


class ChangeReport(_Output):
    summary: Text
    changes: tuple[Change, ...] = Field(default=(), max_length=50)
    verifications: tuple[Verification, ...] = Field(default=(), max_length=20)
    risks: tuple[Short, ...] = Field(default=(), max_length=20)


def _check_change_report(output: _Output, facts: RunFacts) -> list[str]:
    assert isinstance(output, ChangeReport)  # noqa: S101  # dispatched by schema
    problems: list[str] = []
    changed = facts.changes()
    for change in output.changes:
        if _norm(change.file) not in changed:
            problems.append(
                f"{change.file}: this run did not change it (no successful patch/write)"
            )
    last_change = max(changed.values(), default=-1)
    for verification in output.verifications:
        call = facts.call(verification.invocation_id)
        if call is None:
            problems.append(f"verification {verification.invocation_id}: no such tool call")
            continue
        if call.name != verification.command:
            problems.append(
                f"verification {verification.invocation_id} ran {call.name}, "
                f"not {verification.command}"
            )
        recorded = {ToolOutcome.SUCCESS: "passed", ToolOutcome.ERROR: "failed"}.get(call.outcome)
        if recorded is None:
            problems.append(
                f"verification {verification.invocation_id} did not run to an outcome "
                f"({call.outcome.value})"
            )
        elif recorded != verification.outcome:
            problems.append(
                f"verification {verification.invocation_id} {recorded}; "
                f"it is reported as {verification.outcome}"
            )
        if verification.outcome == "passed" and call.order < last_change:
            problems.append(
                f"verification {verification.invocation_id} ran before the last change; "
                "run it again to verify the change"
            )
    return problems


CHANGE_REPORT = Contract(
    id="change-report",
    version="1.0.0",
    name="Change report",
    description="What was changed and how it was verified.",
    schema=ChangeReport,
    rules=(
        "every listed file was changed by a successful patch or write in this run",
        "every verification names a tool call of this run (invocation id and tool name) and "
        "its recorded outcome: passed if it succeeded, failed if it reported an error",
        "a passing verification ran after the last change",
    ),
    check=_check_change_report,
)


# code-review --------------------------------------------------------------------------------------


class Finding(_Output):
    file: FilePath
    line: int = Field(ge=1)
    end_line: int | None = Field(default=None, ge=1)
    severity: Literal["critical", "major", "minor", "nit"]
    title: Short
    detail: Text
    suggestion: str = Field(default="", max_length=2000)


class CodeReview(_Output):
    summary: Text
    scope: tuple[FilePath, ...] = Field(default=(), max_length=200)
    """What was reviewed."""
    findings: tuple[Finding, ...] = Field(default=(), max_length=100)


_NUMBERED = re.compile(r"^\s*(\d+) \| ", re.M)
_READ_FOOTER = re.compile(r"^\[(.+?): lines (\d+)-(\d+) of \d+\]$", re.M)
_SEARCH_HIT = re.compile(r"^(.+?):(\d+): ", re.M)
_OUTLINE = re.compile(r"^(\d+): ", re.M)
_DIFF_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.M)
_DIFF_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def seen_lines(facts: RunFacts) -> dict[str, set[int] | None]:
    """File -> line numbers the run was shown (None: the whole file)."""
    seen: dict[str, set[int] | None] = {}

    def add(path: str, lines: Iterable[int] | None) -> None:
        key = _norm(path)
        if lines is None or seen.get(key, set()) is None:
            seen[key] = None
            return
        current = seen.setdefault(key, set())
        assert current is not None  # noqa: S101  # handled above
        current.update(lines)

    for call in facts.calls:
        if call.outcome is not ToolOutcome.SUCCESS:
            continue
        path = call.arguments.get("path")
        if call.name == "code_read" and (footer := _READ_FOOTER.search(call.text)):
            add(footer.group(1), range(int(footer.group(2)), int(footer.group(3)) + 1))
        elif call.name == "fs_read_file" and isinstance(path, str):
            add(path, None)
        elif call.name == "code_outline" and isinstance(path, str):
            add(path, (int(n) for n in _OUTLINE.findall(call.text)))
        elif call.name == "code_search":
            for hit_path, number in _SEARCH_HIT.findall(call.text):
                add(hit_path, [int(number)])
        elif call.name == "git_diff":
            for section in re.split(r"^diff --git ", call.text, flags=re.M)[1:]:
                header = _DIFF_FILE.search(section)
                if header is None:
                    continue
                for start, count in _DIFF_HUNK.findall(section):
                    first = int(start)
                    add(header.group(1), range(first, first + int(count or "1")))
    return seen


def _line_count(root: Path, file: str) -> int | str:
    try:
        path = resolve_within(root, file)
    except ToolError as exc:
        return str(exc)
    if not path.is_file():
        return "no such file"
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError as exc:
        return f"cannot be read ({type(exc).__name__})"


def _check_code_review(output: _Output, facts: RunFacts) -> list[str]:
    assert isinstance(output, CodeReview)  # noqa: S101  # dispatched by schema
    if facts.root is None and output.findings:
        return ["this workspace has no file tools, so findings cannot be checked"]
    assert facts.root is not None or not output.findings  # noqa: S101  # narrowed above
    problems: list[str] = []
    seen = seen_lines(facts)
    for finding in output.findings:
        where = f"{finding.file}:{finding.line}"
        assert facts.root is not None  # noqa: S101  # findings imply a root
        count = _line_count(facts.root, finding.file)
        if isinstance(count, str):
            problems.append(f"{where}: {count}")
            continue
        last = finding.end_line or finding.line
        if last < finding.line:
            problems.append(f"{where}: end_line is before line")
            continue
        if last > count:
            problems.append(f"{where}: the file has {count} lines")
            continue
        lines = seen.get(_norm(finding.file), set())
        if lines is not None and not (set(range(finding.line, last + 1)) & lines):
            problems.append(
                f"{where}: this run was never shown that line (read it with code_read first)"
            )
    return problems


CODE_REVIEW = Contract(
    id="code-review",
    version="1.0.0",
    name="Code review",
    description="Findings that each point to an existing file and line the reviewer read.",
    schema=CodeReview,
    rules=(
        "every finding names a file under the workspace root and a line it has",
        "the run was shown that line (by code_read, fs_read_file, code_search, code_outline or "
        "git_diff)",
    ),
    check=_check_code_review,
)


# research-brief -----------------------------------------------------------------------------------

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
Citation = Annotated[str, Field(pattern=rf"^(S\d{{1,3}}|C-[0-9a-f]{{8}}|{_UUID})$")]


class Claim(_Output):
    statement: Text
    citations: tuple[Citation, ...] = Field(min_length=1, max_length=10)
    """Evidence handles (C-xxxxxxxx, S1) this run received, or the id of a run it delegated
    that completed."""


class Contradiction(_Output):
    description: Text
    citations: tuple[Citation, ...] = Field(min_length=2, max_length=10)


class ResearchBrief(_Output):
    question: Text
    summary: Text
    findings: tuple[Claim, ...] = Field(min_length=1, max_length=50)
    contradictions: tuple[Contradiction, ...] = Field(default=(), max_length=20)
    gaps: tuple[Short, ...] = Field(default=(), max_length=20)
    """What the evidence did not answer."""


def _check_research_brief(output: _Output, facts: RunFacts) -> list[str]:
    assert isinstance(output, ResearchBrief)  # noqa: S101  # dispatched by schema
    problems: list[str] = []
    items: list[tuple[str, Sequence[str]]] = [
        (f"finding {i}", c.citations) for i, c in enumerate(output.findings, start=1)
    ]
    items += [
        (f"contradiction {i}", c.citations) for i, c in enumerate(output.contradictions, start=1)
    ]
    for label, citations in items:
        for citation in citations:
            if citation in facts.evidence:
                continue
            outcome = facts.children.get(citation)
            if outcome == "completed":
                continue
            if outcome is not None:
                problems.append(f"{label}: delegated run {citation} did not complete ({outcome})")
            else:
                problems.append(f"{label}: {citation} is not evidence this run received")
    for i, claim in enumerate(output.findings, start=1):
        if CITATION.search(claim.statement) and not claim.citations:
            problems.append(f"finding {i}: put citations in `citations`, not in the text")
    return problems


RESEARCH_BRIEF = Contract(
    id="research-brief",
    version="1.0.0",
    name="Research brief",
    description="Findings that each cite evidence the run received; contradictions kept.",
    schema=ResearchBrief,
    rules=(
        "every citation is a passage handle this run received (from knowledge tools, its context "
        "or its assignment) or the id of a run it delegated that completed",
        "contradictions cite at least two sources",
    ),
    check=_check_research_brief,
)


# data-analysis ------------------------------------------------------------------------------------


class DataFinding(_Output):
    statement: Text
    value: float | int | str
    """The reported number or value, as it appears in the query result."""
    query_id: str = Field(pattern=r"^Q-[0-9a-f]{8}$")


class DataAnalysis(_Output):
    question: Text
    answer: Text
    findings: tuple[DataFinding, ...] = Field(min_length=1, max_length=50)
    caveats: tuple[Short, ...] = Field(default=(), max_length=20)


def _cells(text: str) -> list[str]:
    cells: list[str] = []
    for line in text.splitlines():
        if line.startswith("|") and not line.startswith("|---"):
            cells += [c.strip().replace("\\|", "|") for c in line.strip().strip("|").split(" | ")]
    return cells


def _decimals(value: float | int | str) -> int:
    text = str(value)
    return len(text.split(".", 1)[1]) if "." in text and "e" not in text.lower() else 0


def value_in(value: float | int | str, text: str) -> bool:
    """Whether a reported value appears in a recorded query result: a cell equal to it, or a
    number that rounds to it at the precision it was reported with."""
    cells = _cells(text)
    if isinstance(value, str):
        try:
            value = float(value) if "." in value else int(value)
        except ValueError:
            return value.strip().casefold() in {c.casefold() for c in cells}
    tolerance = 0.5 * 10 ** -_decimals(value) if isinstance(value, float) else 0.5
    for cell in cells:
        try:
            number = float(cell)
        except ValueError:
            continue
        if abs(number - value) <= tolerance * (1 + 1e-9):
            return True
    return False


def _check_data_analysis(output: _Output, facts: RunFacts) -> list[str]:
    assert isinstance(output, DataAnalysis)  # noqa: S101  # dispatched by schema
    queries = {
        query_id(c.invocation_id): c
        for c in facts.calls
        if c.name == "data_query" and c.outcome is ToolOutcome.SUCCESS
    }
    problems: list[str] = []
    for i, finding in enumerate(output.findings, start=1):
        call = queries.get(finding.query_id)
        if call is None:
            problems.append(f"finding {i}: {finding.query_id} is not a query this run executed")
        elif not value_in(finding.value, call.text):
            problems.append(
                f"finding {i}: {json.dumps(finding.value)} does not appear in the result of "
                f"{finding.query_id}"
            )
    return problems


DATA_ANALYSIS = Contract(
    id="data-analysis",
    version="1.0.0",
    name="Data analysis",
    description="Every reported number comes from a query this run executed.",
    schema=DataAnalysis,
    rules=(
        "every finding names the query_id of a data_query this run executed successfully",
        "its value appears in that query's recorded result (numbers may be rounded)",
    ),
    check=_check_data_analysis,
)


# docs-change --------------------------------------------------------------------------------------


class DocChange(_Output):
    file: FilePath
    summary: Text


class DocsChange(_Output):
    summary: Text
    files: tuple[DocChange, ...] = Field(min_length=1, max_length=50)


def _covers(scope: str, file: str) -> bool:
    return scope == "." or file == scope or file.startswith(scope + "/")


def _check_docs_change(output: _Output, facts: RunFacts) -> list[str]:
    assert isinstance(output, DocsChange)  # noqa: S101  # dispatched by schema
    changed = facts.changes()
    checks = [c for c in facts.calls if c.name == "docs_check" and c.outcome is ToolOutcome.SUCCESS]
    problems: list[str] = []
    for item in output.files:
        file = _norm(item.file)
        if file not in changed:
            problems.append(f"{item.file}: this run did not write it")
            continue
        after = [
            c
            for c in checks
            if c.order > changed[file] and _covers(_norm(str(c.arguments.get("path", "."))), file)
        ]
        if not after:
            problems.append(f"{item.file}: run docs_check on it after the last change")
            continue
        found = [line for line in after[-1].text.splitlines() if line.startswith(file + ":")]
        if found:
            problems.append(f"{item.file}: docs_check found {len(found)} problems: {found[0]}")
    return problems


DOCS_CHANGE = Contract(
    id="docs-change",
    version="1.0.0",
    name="Documentation change",
    description="Every listed file was written in this run and passed docs_check afterwards.",
    schema=DocsChange,
    rules=(
        "every listed file was written by a successful write or patch in this run",
        "docs_check ran on it after its last change and found no problems in it",
    ),
    check=_check_docs_change,
)


# automation-log -----------------------------------------------------------------------------------


class Step(_Output):
    description: Text
    invocation_id: InvocationId
    outcome: Literal["succeeded", "failed", "not_run", "unknown"]
    notes: str = Field(default="", max_length=1000)


class AutomationLog(_Output):
    summary: Text
    plan: tuple[Short, ...] = Field(min_length=1, max_length=50)
    steps: tuple[Step, ...] = Field(default=(), max_length=100)
    unknown_side_effects: tuple[InvocationId, ...] = Field(default=(), max_length=50)
    """Invocations whose effect is unknown: check the system before retrying."""


_STEP_OUTCOME = {
    ToolOutcome.SUCCESS: "succeeded",
    ToolOutcome.ERROR: "failed",
    ToolOutcome.TIMEOUT: "failed",
    ToolOutcome.DENIED: "not_run",
    ToolOutcome.INVALID: "not_run",
    ToolOutcome.AMBIGUOUS: "unknown",
}


def _check_automation_log(output: _Output, facts: RunFacts) -> list[str]:
    assert isinstance(output, AutomationLog)  # noqa: S101  # dispatched by schema
    problems: list[str] = []
    for step in output.steps:
        call = facts.call(step.invocation_id)
        if call is None:
            problems.append(f"step {step.invocation_id!r}: no such tool call in this run")
            continue
        recorded = _STEP_OUTCOME[call.outcome]
        if recorded != step.outcome:
            problems.append(
                f"step {step.invocation_id}: recorded {recorded}, reported {step.outcome}"
            )
    acting = [
        c
        for c in facts.calls
        if c.effect is not None
        and c.effect is not ToolEffect.READ_ONLY
        and c.outcome in (ToolOutcome.SUCCESS, ToolOutcome.ERROR, ToolOutcome.AMBIGUOUS)
        and not c.name.startswith(SUBMIT_PREFIX)
    ]
    reported = {s.invocation_id for s in output.steps}
    problems += [
        f"{c.name} ({c.invocation_id}) changed or may have changed something; report it as a step"
        for c in acting
        if c.invocation_id not in reported
    ]
    unknown = {c.invocation_id for c in facts.calls if c.outcome is ToolOutcome.AMBIGUOUS}
    problems += [
        f"{i}: its outcome is unknown; list it in unknown_side_effects"
        for i in sorted(unknown - set(output.unknown_side_effects))
    ]
    problems += [
        f"{i}: its outcome is known; remove it from unknown_side_effects"
        for i in sorted(set(output.unknown_side_effects) - unknown)
    ]
    return problems


AUTOMATION_LOG = Contract(
    id="automation-log",
    version="1.0.0",
    name="Automation log",
    description="Every step names a tool call of this run with its real outcome.",
    schema=AutomationLog,
    rules=(
        "every step names a tool call of this run and its recorded outcome (succeeded, failed, "
        "not_run when denied or invalid, unknown when its effect is unknown)",
        "every call that changed or may have changed something is a step",
        "exactly the calls with unknown outcome are listed in unknown_side_effects",
    ),
    check=_check_automation_log,
)


CONTRACTS: dict[str, Contract] = {
    c.id: c
    for c in (
        CHANGE_REPORT,
        CODE_REVIEW,
        RESEARCH_BRIEF,
        DATA_ANALYSIS,
        DOCS_CHANGE,
        AUTOMATION_LOG,
    )
}
