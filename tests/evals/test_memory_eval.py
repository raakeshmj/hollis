"""Memory and context evaluation (offline, deterministic). See docs/architecture/evaluation.md.

Thresholds are set below the measured results so regressions fail; the measured numbers are
recorded in the evaluation document, regenerated with `uv run python -m tests.evals.report`.
"""

from pathlib import Path

import pytest

from tests.evals.compaction import naive_replace_notes_kept, run_compaction
from tests.evals.harness import Report, evaluate
from tests.evals.scenario import build, build_research

BUDGET = 1200


def _common_checks(report: Report) -> None:
    assert report.violation_rate("memory", ("isolation",)) == 0, "another user's memory leaked"
    assert report.violation_rate("memory", ("forgetting",)) == 0, "a deleted memory was offered"
    stale = report.violation_rate("memory", ("knowledge_update", "temporal", "temporary"))
    assert stale == 0, "superseded, corrected or expired evidence offered as current"
    assert report.mean_tokens("memory") <= BUDGET
    assert report.rate("memory", "clean") >= report.rate("raw_history", "clean") + 0.4


@pytest.mark.parametrize("seed", [3, 7, 11])
@pytest.mark.parametrize("noise", [10, 40])
async def test_ops_scenario_memory_beats_baselines(seed: int, noise: int) -> None:
    report = await evaluate(build(noise_per_session=noise, seed=seed), budget_tokens=BUDGET)
    _common_checks(report)
    assert report.rate("memory", "clean") >= 0.95


async def test_ops_scenario_on_sqlite(tmp_path: Path) -> None:
    report = await evaluate(build(noise_per_session=40), store_kind="sqlite", directory=tmp_path)
    _common_checks(report)
    assert report.rate("memory", "clean") >= 0.95
    assert max(report.recall_ms) < 250  # generous: CI machines vary


async def test_held_out_research_scenario() -> None:
    """Written after retrieval tuning and never tuned against (see scenario.build_research)."""
    report = await evaluate(build_research(noise_per_session=40), budget_tokens=BUDGET)
    _common_checks(report)
    assert report.rate("memory", "clean") >= 0.85


async def test_regression_machine_identifier_reaches_the_procedure() -> None:
    """M4's known gap, closed in M5 by identifier-aware analysis (ADR-0023): the log line's
    `CUDA_ERROR_OUT_OF_MEMORY` now links to the procedure about "CUDA out of memory"."""
    report = await evaluate(build_research(noise_per_session=40), budget_tokens=BUDGET)
    causal = next(r for r in report.for_condition("memory") if r.probe.pid == "causal")
    assert causal.clean


async def test_no_memory_and_context_only_lack_cross_session_evidence() -> None:
    report = await evaluate(build(noise_per_session=40), budget_tokens=BUDGET)
    assert report.rate("no_memory", "evidence") == 0
    assert report.rate("context_only", "evidence") == 0


@pytest.mark.parametrize("summarizer", ["deterministic", "forgetful"])
async def test_compaction_keeps_actions_notes_and_the_objective(summarizer: str) -> None:
    result = await run_compaction(summarizer)
    assert result.compactions >= 3
    assert result.actions_kept == 1.0
    assert result.notes_kept == 1.0
    assert result.input_always_present
    assert result.within_budget
    assert result.final_tokens * 4 < result.full_history_tokens


async def test_carry_forward_beats_naive_replacement() -> None:
    result = await run_compaction("forgetful")
    assert naive_replace_notes_kept(result.drafts) <= 0.5 < result.notes_kept
