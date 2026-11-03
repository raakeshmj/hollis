"""Output contracts are checked against what the run's log recorded, never the model's account.

Each test builds the recorded facts (`RunFacts`) a run would have and submits an output that is
honest, then one that misreports something; the checks must accept the first and name the
problem in the second.
"""

from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue, ValidationError

from hollis.runtime.events import ToolOutcome
from hollis.runtime.tools import ToolEffect
from hollis.specialists.contracts import CONTRACTS, Call, RunFacts, value_in
from hollis.tools.builtin.data import query_id

RESULT = (
    "query_id: {qid}\ntables: sales\nrows: 2\n| region | total |\n|---|---|\n"
    "| north | 17.25 |\n| south | 32 |"
)


def _call(
    order: int,
    name: str,
    *,
    outcome: ToolOutcome = ToolOutcome.SUCCESS,
    text: str = "",
    effect: ToolEffect = ToolEffect.READ_ONLY,
    invocation: str | None = None,
    data: tuple[JsonValue, ...] = (),
    **arguments: JsonValue,
) -> Call:
    return Call(
        order=order,
        invocation_id=invocation or f"inv-{order}",
        name=name,
        arguments=dict(arguments),
        outcome=outcome,
        text=text,
        effect=effect,
        data=data,
    )


def _check(contract: str, output: dict[str, Any], facts: RunFacts) -> list[str]:
    spec = CONTRACTS[contract]
    return spec.check(spec.schema.model_validate(output), facts)


def _facts(*calls: Call, root: Path | None = None, **kwargs: Any) -> RunFacts:
    return RunFacts(calls=calls, evidence=kwargs.pop("evidence", {}), root=root, **kwargs)


# change-report --------------------------------------------------------------------------------


def _engineering() -> RunFacts:
    return _facts(
        _call(0, "code_read", path="src/a.py"),
        _call(
            1,
            "run_tests",
            outcome=ToolOutcome.ERROR,
            text="exit code 1",
            effect=ToolEffect.MUTATING,
        ),
        _call(2, "apply_patch", effect=ToolEffect.MUTATING, patch="...", paths=["src/a.py"]),
        _call(
            3,
            "apply_patch",
            outcome=ToolOutcome.DENIED,
            effect=ToolEffect.MUTATING,
            paths=["src/b.py"],
        ),
        _call(4, "run_tests", text="5 passed", effect=ToolEffect.MUTATING),
    )


def test_an_honest_change_report_is_accepted() -> None:
    report = {
        "summary": "Fixed the bound.",
        "changes": [{"file": "./src/a.py", "summary": "use <="}],
        "verifications": [
            {"invocation_id": "inv-4", "command": "run_tests", "outcome": "passed"},
            {"invocation_id": "inv-1", "command": "run_tests", "outcome": "failed"},
        ],
    }
    assert _check("change-report", report, _engineering()) == []


@pytest.mark.parametrize(
    ("change", "verification", "problem"),
    [
        ({"file": "src/b.py", "summary": "x"}, None, "did not change it"),  # the patch was denied
        ({"file": "src/c.py", "summary": "x"}, None, "did not change it"),  # never touched
        (None, {"invocation_id": "inv-1", "command": "run_tests", "outcome": "passed"}, "failed;"),
        (None, {"invocation_id": "inv-4", "command": "lint", "outcome": "passed"}, "not lint"),
        (None, {"invocation_id": "inv-9", "command": "run_tests", "outcome": "passed"}, "no such"),
        (
            None,
            {"invocation_id": "inv-3", "command": "apply_patch", "outcome": "failed"},
            "did not run",
        ),
    ],
)
def test_a_misreported_change_report_is_rejected(
    change: dict[str, str] | None, verification: dict[str, str] | None, problem: str
) -> None:
    report: dict[str, Any] = {
        "summary": "s",
        "changes": [change] if change else [],
        "verifications": [verification] if verification else [],
    }
    problems = _check("change-report", report, _engineering())
    assert any(problem in p for p in problems), problems


def test_a_check_that_ran_before_the_last_change_does_not_verify_it() -> None:
    facts = _facts(
        _call(0, "run_tests", text="ok", effect=ToolEffect.MUTATING),
        _call(1, "apply_patch", effect=ToolEffect.MUTATING, paths=["a.py"]),
    )
    report = {
        "summary": "s",
        "changes": [{"file": "a.py", "summary": "x"}],
        "verifications": [{"invocation_id": "inv-0", "command": "run_tests", "outcome": "passed"}],
    }
    assert any("before the last change" in p for p in _check("change-report", report, facts))


# code-review ----------------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("".join(f"line {n}\n" for n in range(1, 41)))
    return tmp_path


def test_review_findings_must_point_to_lines_the_run_was_shown(repo: Path) -> None:
    facts = _facts(
        _call(0, "code_read", text="10 | line 10\n11 | line 11\n[src/a.py: lines 10-11 of 40]"),
        _call(1, "code_search", text="src/a.py:30: line 30\n1 matches in 1 files scanned"),
        root=repo,
    )

    def finding(line: int, file: str = "src/a.py") -> dict[str, Any]:
        return {"file": file, "line": line, "severity": "minor", "title": "t", "detail": "d"}

    review = {"summary": "s", "findings": [finding(11), finding(30)]}
    assert _check("code-review", review, facts) == []
    for bad, problem in [
        (finding(20), "never shown"),
        (finding(41), "has 40 lines"),
        (finding(1, "src/missing.py"), "no such file"),
        (finding(1, "../outside.py"), "escapes"),
        (finding(1, ".git/config"), "protected"),
    ]:
        problems = _check("code-review", {"summary": "s", "findings": [bad]}, facts)
        assert any(problem in p for p in problems), (bad, problems)


def test_whole_file_reads_and_diffs_count_as_seen(repo: Path) -> None:
    diff = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -5,2 +5,3 @@\n line 5\n+new\n line 6\n"
    )
    facts = _facts(_call(0, "git_diff", text=diff), root=repo)
    finding = {"file": "src/a.py", "line": 6, "severity": "nit", "title": "t", "detail": "d"}
    assert _check("code-review", {"summary": "s", "findings": [finding]}, facts) == []
    outside = {**finding, "line": 30}
    assert _check("code-review", {"summary": "s", "findings": [outside]}, facts)
    read_all = _facts(_call(0, "fs_read_file", path="src/a.py"), root=repo)
    assert _check("code-review", {"summary": "s", "findings": [outside]}, read_all) == []


def test_an_empty_review_is_valid() -> None:
    assert _check("code-review", {"summary": "nothing found"}, _facts()) == []


# research-brief --------------------------------------------------------------------------------


def test_research_citations_must_be_evidence_the_run_received() -> None:
    child = "6f1c2a3b-0000-4000-8000-000000000001"
    failed = "6f1c2a3b-0000-4000-8000-000000000002"
    facts = _facts(
        evidence={"C-0123abcd": "Keys rotate every 90 days."},
        children={child: "completed", failed: "failed"},
    )
    brief = {
        "question": "q",
        "summary": "s",
        "findings": [
            {"statement": "Keys rotate every 90 days.", "citations": ["C-0123abcd"]},
            {"statement": "The analyst counted 12 stale keys.", "citations": [child]},
        ],
    }
    assert _check("research-brief", brief, facts) == []
    fabricated = {**brief, "findings": [{"statement": "x", "citations": ["C-deadbeef"]}]}
    assert any("not evidence" in p for p in _check("research-brief", fabricated, facts))
    unfinished = {**brief, "findings": [{"statement": "x", "citations": [failed]}]}
    assert any("did not complete" in p for p in _check("research-brief", unfinished, facts))


def test_research_findings_need_citations_and_contradictions_two() -> None:
    with pytest.raises(ValidationError):
        CONTRACTS["research-brief"].schema.model_validate(
            {"question": "q", "summary": "s", "findings": [{"statement": "x", "citations": []}]}
        )
    with pytest.raises(ValidationError):
        CONTRACTS["research-brief"].schema.model_validate(
            {
                "question": "q",
                "summary": "s",
                "findings": [{"statement": "x", "citations": ["S1"]}],
                "contradictions": [{"description": "d", "citations": ["S1"]}],
            }
        )


# data-analysis ----------------------------------------------------------------------------------


def test_numbers_must_come_from_a_query_this_run_executed() -> None:
    qid = query_id("inv-0")
    facts = _facts(_call(0, "data_query", text=RESULT.format(qid=qid)))
    analysis = {
        "question": "q",
        "answer": "a",
        "findings": [
            {"statement": "north total", "value": 17.25, "query_id": qid},
            {"statement": "north rounded", "value": 17.3, "query_id": qid},
            {"statement": "south", "value": "32", "query_id": qid},
            {"statement": "region", "value": "North", "query_id": qid},
        ],
    }
    assert _check("data-analysis", analysis, facts) == []
    for value, query, problem in [
        (18, qid, "does not appear"),
        (17.1, qid, "does not appear"),
        (17.25, "Q-00000000", "not a query"),
    ]:
        bad = {**analysis, "findings": [{"statement": "s", "value": value, "query_id": query}]}
        assert any(problem in p for p in _check("data-analysis", bad, facts)), (value, query)


def test_a_failed_query_cites_nothing() -> None:
    qid = query_id("inv-0")
    facts = _facts(_call(0, "data_query", outcome=ToolOutcome.ERROR, text=RESULT.format(qid=qid)))
    bad = {
        "question": "q",
        "answer": "a",
        "findings": [{"statement": "s", "value": 32, "query_id": qid}],
    }
    assert _check("data-analysis", bad, facts)


@pytest.mark.parametrize(
    ("value", "found"),
    [
        (17.25, True),
        (17.1, False),
        (17.2, True),  # 17.25 is exactly halfway: either rounding is honest,
        (17.3, True),
        (17, True),
        (32.0, True),
        (33, False),
        ("south", True),
    ],
)
def test_value_matching(value: float | int | str, found: bool) -> None:
    assert value_in(value, RESULT.format(qid="Q-1")) is found


# docs-change -------------------------------------------------------------------------------------


def test_docs_must_be_written_then_pass_docs_check() -> None:
    clean = "1 files checked, 0 problems: no problems found"
    facts = _facts(
        _call(0, "docs_check", text=clean, path="docs"),
        _call(1, "fs_write_file", effect=ToolEffect.MUTATING, path="docs/a.md"),
        _call(2, "fs_write_file", effect=ToolEffect.MUTATING, path="docs/b.md"),
        _call(3, "docs_check", text=clean, path="docs/a.md"),
        _call(
            4,
            "docs_check",
            text="docs/b.md:3: broken link: x.md does not exist\n1 files checked, 1 problems",
            path="docs",
        ),
    )
    change = {"summary": "s", "files": [{"file": "docs/a.md", "summary": "new"}]}
    assert _check("docs-change", change, facts) == []
    for file, problem in [("docs/b.md", "found 1 problems"), ("docs/c.md", "did not write")]:
        bad = {"summary": "s", "files": [{"file": file, "summary": "x"}]}
        assert any(problem in p for p in _check("docs-change", bad, facts)), file
    only_before = _facts(facts.calls[0], facts.calls[1])
    assert any("after the last change" in p for p in _check("docs-change", change, only_before))


# automation-log ----------------------------------------------------------------------------------


def test_every_step_has_its_recorded_outcome_and_unknowns_are_listed() -> None:
    facts = _facts(
        _call(0, "fs_read_file", path="runbook.md"),
        _call(1, "rotate_logs", effect=ToolEffect.MUTATING, text="rotated"),
        _call(2, "restart", outcome=ToolOutcome.AMBIGUOUS, effect=ToolEffect.MUTATING),
        _call(3, "purge", outcome=ToolOutcome.DENIED, effect=ToolEffect.DESTRUCTIVE),
    )
    steps: list[dict[str, str]] = [
        {"description": "rotate", "invocation_id": "inv-1", "outcome": "succeeded"},
        {"description": "restart", "invocation_id": "inv-2", "outcome": "unknown"},
        {"description": "purge", "invocation_id": "inv-3", "outcome": "not_run"},
    ]
    log: dict[str, Any] = {
        "summary": "s",
        "plan": ["rotate", "restart"],
        "steps": steps,
        "unknown_side_effects": ["inv-2"],
    }
    assert _check("automation-log", log, facts) == []
    claims_success = {
        **log,
        "steps": [steps[0], {**steps[1], "outcome": "succeeded"}],
    }
    assert any("recorded unknown" in p for p in _check("automation-log", claims_success, facts))
    hides_unknown: dict[str, Any] = {**log, "unknown_side_effects": []}
    assert any(
        "list it in unknown_side_effects" in p
        for p in _check("automation-log", hides_unknown, facts)
    )
    omits_step = {**log, "steps": steps[1:]}
    assert any("report it as a step" in p for p in _check("automation-log", omits_step, facts))


def test_every_contract_explains_its_checks_and_has_a_submit_tool() -> None:
    for contract in CONTRACTS.values():
        assert contract.rules
        assert contract.tool_name == "submit_" + contract.id.replace("-", "_")
        assert contract.tool_name in contract.guidance()
        assert contract.schema.model_json_schema()["type"] == "object"
