"""Specialist evaluation (offline: deterministic simulated agent on the real platform).
See docs/architecture/specialist-evaluation.md; regenerate the tables with
`uv run python -m tests.evals.specialists.report`.

These assertions pin mechanisms, including where specialization does not help or costs more.
They run a smaller grid than the report (fewer seeds), with the same code. Wall-clock time is
reported, not asserted.
"""

import asyncio

import pytest

from tests.evals.specialists.harness import CONDITIONS, Result, evaluate, summary

G, GT, S = CONDITIONS


type Table = dict[tuple[str, str, float, float], dict[str, float]]


@pytest.fixture(scope="module")
def table(tmp_path_factory: pytest.TempPathFactory) -> Table:
    """One evaluation for the whole module, on its own event loop."""
    root = tmp_path_factory.mktemp("specialists")
    results: list[Result] = asyncio.run(evaluate(root, ps=(0.0, 0.3), rs=(0.0, 1.0), seeds=6))
    return summary(results)


def _cell(
    table: Table,
    task: str,
    condition: str,
    p: float = 0.0,
    r: float = 1.0,
) -> dict[str, float]:
    return table[(task, condition, p, r)]


TASKS = ("data, 40 rows", "data, 3,000 rows", "code review", "research", "bug fix")


def test_without_errors_every_condition_that_can_do_the_task_does_it(table: Table) -> None:
    for task in TASKS:
        for condition in CONDITIONS:
            cell = _cell(table, task, condition)
            if (task, condition) == ("data, 3,000 rows", G):
                assert cell["correct"] == 0, "the file does not fit in one tool result"
                assert cell["flagged"] == 1, "...and the generalist says so: no silent error"
            else:
                assert cell["correct"] == 1, (task, condition)


def test_specialization_costs_tokens(table: Table) -> None:
    """Longer instructions, a contract schema and a submission turn are paid on every run."""
    for task in TASKS:
        g, s = _cell(table, task, G), _cell(table, task, S)
        assert s["tokens"] > g["tokens"], task
        assert s["model_calls"] >= g["model_calls"], task


def test_specialists_are_offered_less_that_can_change_things(table: Table) -> None:
    for task in ("data, 40 rows", "code review", "research"):
        assert _cell(table, task, S)["mutating"] == 0, task
        assert _cell(table, task, G)["mutating"] >= 1, task
    review = (_cell(table, "code review", c, 0.3, 0.0) for c in CONDITIONS)
    g, gt, s = review
    assert g["approvals"] > 0, "a generalist's unrequested write reaches a person"
    assert gt["approvals"] > 0
    assert s["approvals"] == 0, "the read-only specialist has nothing to ask for"


def test_contracts_turn_detectable_errors_into_visible_failures(table: Table) -> None:
    """With no fixing at all (r = 0), every detectable error the specialist made was caught,
    so it never delivered a wrong result from a detectable error."""
    for task in ("data, 40 rows", "data, 3,000 rows", "research", "code review"):
        s, gt = _cell(table, task, S, 0.3, 0.0), _cell(table, task, GT, 0.3, 0.0)
        assert s["caught"] == 1, task
        assert s["silent"] <= gt["silent"], task
        assert s["flagged"] > gt["flagged"], task


def test_feedback_turns_caught_errors_into_correct_results(table: Table) -> None:
    for task in TASKS:
        s0, s1 = _cell(table, task, S, 0.3, 0.0), _cell(table, task, S, 0.3, 1.0)
        assert s1["correct"] >= s0["correct"], task
        assert s1["correct"] >= _cell(table, task, GT, 0.3, 1.0)["correct"], task


def test_errors_a_contract_cannot_see_stay_silent_in_every_condition(table: Table) -> None:
    """A wrong leading region (reasoning), a near-miss line, a made-up claim with a real
    citation: references check out, so even a perfect fixer delivers them."""
    for task in ("data, 40 rows", "code review", "research"):
        assert _cell(table, task, S, 0.3, 1.0)["silent"] > 0, task


def test_tools_decide_what_is_possible_at_all(table: Table) -> None:
    assert _cell(table, "data, 3,000 rows", GT)["correct"] == 1
    assert _cell(table, "data, 3,000 rows", G)["correct"] == 0
    g, gt = _cell(table, "bug fix", G, 0.3, 1.0), _cell(table, "bug fix", GT, 0.3, 1.0)
    assert gt["correct"] >= g["correct"], "with a test command a wrong patch gets noticed"
