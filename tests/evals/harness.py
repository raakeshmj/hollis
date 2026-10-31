"""Memory evaluation harness: conditions, evidence scoring and metrics.

What this measures, precisely: whether the context Hollis would give a model contains the
evidence a probe needs (and none of the evidence it must not rely on), within a fixed input
token budget. That is *context sufficiency*, a model-free proxy. It does not measure whether a
particular model then answers correctly; the opt-in live evaluation does that.

Conditions, all at the same token budget:
- `no_memory`: nothing beyond the question.
- `context_only`: the current session's statements only (what a fresh run would have).
- `raw_history`: every earlier statement in order, newest kept when the budget is exceeded
  (the long-context baseline under a budget).
- `memory`: statements written through the memory write path (user statements as confirmed
  facts with their slot semantics, tool output as observed, deletions as forget), then recalled
  for the question as of the probe time.
"""

import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from hollis.adapters.sqlite_memory import SqliteMemoryStore
from hollis.memory import (
    InMemoryMemoryStore,
    MemoryCandidate,
    MemoryManager,
    MemoryStore,
    Provenance,
    RecallQuery,
    SourceKind,
)
from hollis.runtime.context import CHARS_PER_TOKEN, estimate_text
from hollis.runtime.memory import Certainty, MemoryAccess
from tests.evals.scenario import Probe, Scenario, Source, Statement, probe_time

CONDITIONS = ("no_memory", "context_only", "raw_history", "memory")
STALE_CATEGORIES = ("knowledge_update", "temporal", "temporary")
ALICE = MemoryAccess(read=("user:alice", "workspace:acme"), write=("user:alice",))
BOB = MemoryAccess(read=("user:bob",), write=("user:bob",))


class EvalClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class ProbeResult:
    probe: Probe
    condition: str
    tokens: int
    present: set[str]

    @property
    def evidence(self) -> bool:
        return set(self.probe.required) <= self.present

    @property
    def violations(self) -> set[str]:
        return set(self.probe.forbidden) & self.present

    @property
    def clean(self) -> bool:
        return self.evidence and not self.violations


@dataclass
class Report:
    scenario: str
    budget_tokens: int
    results: list[ProbeResult] = field(default_factory=list[ProbeResult])
    recall_ms: list[float] = field(default_factory=list[float])
    records: int = 0
    store_bytes: int | None = None
    ingest_ms: float = 0.0

    def for_condition(self, condition: str) -> list[ProbeResult]:
        return [r for r in self.results if r.condition == condition]

    def rate(self, condition: str, attribute: str, category: str | None = None) -> float:
        rows = [
            r
            for r in self.for_condition(condition)
            if (category is None or r.probe.category == category)
            and (attribute != "evidence" or r.probe.required)
        ]
        if not rows:
            return float("nan")
        return sum(bool(getattr(r, attribute)) for r in rows) / len(rows)

    def violation_rate(self, condition: str, categories: Sequence[str]) -> float:
        rows = [r for r in self.for_condition(condition) if r.probe.category in categories]
        return sum(bool(r.violations) for r in rows) / len(rows) if rows else float("nan")

    def mean_tokens(self, condition: str) -> float:
        return statistics.fmean(r.tokens for r in self.for_condition(condition))


def _present(scenario: Scenario, text: str) -> set[str]:
    return {s.sid for s in scenario.statements if s.text in text}


def window(lines: list[str], budget_tokens: int) -> str:
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        cost = estimate_text(line) + 1
        if used + cost > budget_tokens:
            break
        kept.append(line)
        used += cost
    return "\n".join(reversed(kept))


def history_lines(scenario: Scenario, probe: Probe, *, current_only: bool) -> list[str]:
    last = probe.session if probe.session is not None else scenario.sessions
    lines: list[str] = []
    for s in scenario.statements:
        if s.owner != "alice" or s.session > last:
            continue
        if current_only and s.session != min(last, scenario.sessions - 1):
            continue
        lines.append(f"[session {s.session}] {s.text}")
    return lines


def _candidate(s: Statement) -> MemoryCandidate:
    tool = s.source is Source.TOOL
    return MemoryCandidate(
        scope=f"user:{s.owner}",
        kind=s.kind,
        content=s.text,
        subject=s.subject,
        key=s.key,
        change=s.change,
        certainty=Certainty.OBSERVED if tool else Certainty.CONFIRMED,
        provenance=Provenance(kind=SourceKind.TOOL if tool else SourceKind.USER, actor=s.owner),
        valid_until=s.at + timedelta(days=s.valid_days) if s.valid_days else None,
    )


async def open_store(kind: str, directory: Path | None) -> MemoryStore:
    if kind == "sqlite":
        if directory is None:
            raise ValueError("sqlite needs a directory")
        return await SqliteMemoryStore.open(directory / "eval-memory.db")
    return InMemoryMemoryStore()


async def ingest(
    scenario: Scenario, manager: MemoryManager, clock: EvalClock, until: int
) -> dict[str, str]:
    """Write statements of sessions <= `until` through the memory write path. Returns
    statement id -> memory id for statements that were stored."""
    ids: dict[str, str] = {}
    forget_next: list[tuple[int, str, MemoryAccess]] = []
    for s in scenario.statements:
        if s.session > until:
            break
        for session, memory_id, access in [f for f in forget_next if f[0] <= s.session]:
            clock.now = s.at
            await manager.forget(memory_id, access, reason="user asked", actor="alice")
            forget_next.remove((session, memory_id, access))
        clock.now = s.at
        access = ALICE if s.owner == "alice" else BOB
        outcome = await manager.remember(_candidate(s), access)
        if outcome.record is not None:
            ids[s.sid] = outcome.record.memory_id
            if s.forget:
                forget_next.append((s.session + 1, outcome.record.memory_id, access))
    return ids


async def evaluate(
    scenario: Scenario,
    *,
    budget_tokens: int = 1200,
    store_kind: str = "memory",
    directory: Path | None = None,
) -> Report:
    report = Report(scenario=scenario.name, budget_tokens=budget_tokens)
    by_session: dict[int, list[Probe]] = {}
    for probe in scenario.probes:
        by_session.setdefault(
            probe.session if probe.session is not None else scenario.sessions, []
        ).append(probe)

    for probe in scenario.probes:
        for condition, text in (
            ("no_memory", ""),
            (
                "context_only",
                window(history_lines(scenario, probe, current_only=True), budget_tokens),
            ),
            (
                "raw_history",
                window(history_lines(scenario, probe, current_only=False), budget_tokens),
            ),
        ):
            report.results.append(
                ProbeResult(probe, condition, estimate_text(text), _present(scenario, text))
            )

    # Memory: one store per probe time, ingesting history up to that session (as a deployed
    # agent would have it), so temporal probes see only what had been learned.
    for until, probes in sorted(by_session.items()):
        target = directory / f"s{until}" if directory is not None else None
        if target is not None:
            target.mkdir(parents=True, exist_ok=True)
        store = await open_store(store_kind, target)
        clock = EvalClock(scenario.statements[0].at)
        manager = MemoryManager(store, clock=clock)
        started = time.perf_counter()
        await ingest(scenario, manager, clock, until)
        report.ingest_ms = max(report.ingest_ms, (time.perf_counter() - started) * 1000)
        for probe in probes:
            clock.now = probe_time(scenario, probe)
            started = time.perf_counter()
            recalled = await manager.recall(
                RecallQuery(
                    text=probe.question,
                    scopes=ALICE.read,
                    limit=10,
                    max_chars=int(budget_tokens * CHARS_PER_TOKEN),
                )
            )
            report.recall_ms.append((time.perf_counter() - started) * 1000)
            text = "\n".join(m.render() for m in recalled)
            report.results.append(
                ProbeResult(probe, "memory", estimate_text(text), _present(scenario, text))
            )
        if until == scenario.sessions:
            report.records = len(await store.records())
            if target is not None:
                report.store_bytes = (target / "eval-memory.db").stat().st_size
    return report


def markdown(reports: Sequence[Report]) -> str:
    lines: list[str] = []
    for report in reports:
        lines.append(
            f"### {report.scenario} (budget {report.budget_tokens} tokens, "
            f"{len({r.probe.pid for r in report.results})} probes)"
        )
        lines.append("")
        lines.append(
            "| condition | evidence | clean | stale/wrong offered | leakage | deleted offered "
            "| mean tokens |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for condition in CONDITIONS:
            lines.append(
                f"| {condition} | {report.rate(condition, 'evidence'):.0%} "
                f"| {report.rate(condition, 'clean'):.0%} "
                f"| {report.violation_rate(condition, STALE_CATEGORIES):.0%} "
                f"| {report.violation_rate(condition, ('isolation',)):.0%} "
                f"| {report.violation_rate(condition, ('forgetting',)):.0%} "
                f"| {report.mean_tokens(condition):.0f} |"
            )
        lines.append("")
        categories = sorted({r.probe.category for r in report.results})
        lines.append("| category | " + " | ".join(CONDITIONS) + " |")
        lines.append("|---|" + "---|" * len(CONDITIONS))
        for category in categories:
            cells = [f"{report.rate(c, 'clean', category):.0%}" for c in CONDITIONS]
            lines.append(f"| {category} | " + " | ".join(cells) + " |")
        lines.append("")
        recall = sorted(report.recall_ms)
        p95 = recall[max(int(len(recall) * 0.95) - 1, 0)] if recall else 0.0
        storage = f", store {report.store_bytes / 1024:.0f} KiB" if report.store_bytes else ""
        lines.append(
            f"Memory: {report.records} records after ingest, recall p50 "
            f"{statistics.median(recall):.1f} ms / p95 {p95:.1f} ms, "
            f"ingest {report.ingest_ms:.0f} ms{storage}."
        )
        lines.append("")
    return "\n".join(lines)
