"""Retrieval and grounding evaluation (offline, deterministic).

See docs/architecture/rag-evaluation.md.

Thresholds sit below the measured results so regressions fail; numbers are recorded in the
document and regenerated with `uv run python -m tests.evals.rag.report`. Dense results use the
local hashing embedder or a SIMULATED semantic embedder; neither is evidence of real embedding
model quality.
"""

import asyncio

import pytest

from hollis.runtime.grounding import check
from tests.evals.rag.corpus import clinical_ops, engineering
from tests.evals.rag.grounding_cases import CASES, EVIDENCE
from tests.evals.rag.harness import ConfigResult, Report, evaluate

DEFAULT = "DEFAULT: hybrid rrf + exact-first + follow-up (hashing)"


@pytest.fixture(scope="module")
def dev(tmp_path_factory: pytest.TempPathFactory) -> Report:
    return asyncio.run(evaluate(engineering(), tmp_path_factory.mktemp("dev")))


@pytest.fixture(scope="module")
def held_out(tmp_path_factory: pytest.TempPathFactory) -> Report:
    return asyncio.run(evaluate(clinical_ops(), tmp_path_factory.mktemp("held-out")))


def _config(report: Report, name: str) -> ConfigResult:
    return next(c for c in report.configs if c.name == name)


def _recall(config: ConfigResult, qid: str) -> float:
    return next(r.recall for r in config.results if r.query.qid == qid)


def test_default_configuration_on_the_development_corpus(dev: Report) -> None:
    default = _config(dev, DEFAULT)
    assert default.mean("recall") >= 0.95
    assert default.mean("rr") >= 0.8
    assert default.mean("ndcg") >= 0.8
    m4 = _config(dev, "lexical (M4-style terms)")
    assert default.mean("recall") >= m4.mean("recall")
    assert default.mean("ndcg") >= m4.mean("ndcg")
    assert default.latency(0.95) < 250  # generous: CI machines vary


def test_exact_identifiers_survive_fusion(dev: Report) -> None:
    for config in dev.configs:
        if "exact-first" in config.name or config.name.startswith("DEFAULT"):
            for category in ("exact_identifier", "error_code", "version"):
                assert config.mean("recall", category) == 1.0, (config.name, category)
    assert _config(dev, "DEFAULT config (SIMULATED semantic)").exact_rate() == 1.0


def test_regressions_and_multi_hop(dev: Report) -> None:
    default = _config(dev, DEFAULT)
    assert _recall(default, "cuda-regression") == 1.0  # M4 known gap, now a regression case
    assert _recall(default, "multi-hop") == 1.0
    assert (
        _recall(_config(dev, "hybrid rrf + exact-first (hashing)"), "multi-hop") == 0.5
    )  # without follow-up


def test_dense_alone_is_not_enough(dev: Report, held_out: Report) -> None:
    for report in (dev, held_out):
        dense = _config(report, "dense (hashing)").mean("recall")
        assert dense <= _config(report, DEFAULT).mean("recall")
    assert _config(held_out, "dense (hashing)").mean("recall") < _config(held_out, DEFAULT).mean(
        "recall"
    )


def test_held_out_corpus(held_out: Report) -> None:
    """Written after the defaults were chosen; never tuned against."""
    assert _config(held_out, DEFAULT).mean("recall") >= 0.9
    assert _config(held_out, DEFAULT).exact_rate() == 1.0


def test_grounding_verifier_agrees_with_labels() -> None:
    claims = agreed = 0
    abstentions_right = 0
    for answer, expected, abstains in CASES:
        report = check(answer, EVIDENCE)
        got = tuple(c.support for c in report.claims)
        claims += max(len(expected), len(got))
        agreed += sum(1 for a, b in zip(got, expected, strict=False) if a is b)
        abstentions_right += report.abstained == abstains
    assert agreed / claims >= 0.85
    assert abstentions_right == len(CASES)
