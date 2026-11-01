"""Delegation between runs: what a child run records about where it came from (ADR-0030).

A delegation is a tool call whose execution is another run. The runtime knows only these
recorded facts; creating, waiting for and reporting on child runs is application logic
(`hollis.platform.orchestration`).

- `Lineage`: the root, parent run and parent invocation of a child run, and its depth.
- `Assignment`: the structured task the child was given, with provenance for any earlier results
  passed to it. The child's run input is the rendered assignment; this keeps the parts apart for
  inspection.
- `DelegationLimits`: the limits that apply to a run and everything below it. A child's limits
  are its parent's, narrowed by its own agent's configuration; they are never widened.

Child run ids are derived from (parent run, invocation), so re-executing an interrupted
delegation finds the existing child instead of creating a second one.

The report text helpers at the end define how a child's answer travels to its parent: one JSON
line of Hollis-observed facts (`REPORT_KIND`), then the answer inside an `<agent-report>` frame
that the answer cannot close, then evidence blocks. Readers (the deterministic coordinator, the
demo model) use these helpers instead of re-parsing ad hoc.
"""

import json
import re
from typing import Any, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

MAX_DEPTH = 5
"""Absolute limit on delegation depth, whatever an agent configures."""

_CHILD_NAMESPACE = UUID("6f1c1d1e-5b8e-4f0a-9d7b-3a1e2c4b5d60")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Lineage(_Frozen):
    root_run_id: UUID
    parent_run_id: UUID
    parent_invocation_id: str = Field(min_length=1)
    depth: int = Field(ge=1, le=MAX_DEPTH)
    """1 for a child of a root run."""


class InputRef(_Frozen):
    """An earlier result passed to a child verbatim (not retyped by the parent)."""

    run_id: UUID
    agent: str | None = None


class Evidence(_Frozen):
    """A piece of evidence handed to a child with an earlier result. Built by the application
    from the recorded, verified evidence of that result, never from model text, so the child's
    grounding check can count citations of it."""

    handle: str = Field(pattern=r"^C-[0-9a-f]{8}$")
    text: str = Field(max_length=4000)
    source: str = Field(default="", max_length=300)


class Assignment(_Frozen):
    agent: str = Field(min_length=1, max_length=64)
    objective: str = Field(min_length=1, max_length=4000)
    context: str = Field(default="", max_length=20_000)
    expected_output: str = Field(default="", max_length=2000)
    inputs: tuple[InputRef, ...] = Field(default=(), max_length=8)
    evidence: tuple[Evidence, ...] = Field(default=(), max_length=32)


class DelegationLimits(_Frozen):
    max_depth: int = Field(default=2, ge=0, le=MAX_DEPTH)
    """Deepest level a run in this tree may have (the root is level 0)."""
    max_children: int = Field(default=8, ge=0, le=64)
    """Delegations per run."""
    max_runs: int = Field(default=24, ge=1, le=500)
    """Runs in the whole tree, including the root."""
    token_budget: int | None = Field(default=None, ge=1)
    """Provider-reported tokens (input + output) across the whole tree. Checked before every
    model call and every delegation; calls already in flight can overshoot by their own size."""
    wait_s: float = Field(default=900.0, gt=0, le=3500)
    """How long a parent waits for one child (also capped by the parent's deadline)."""

    def narrow(self, other: "DelegationLimits | None") -> "DelegationLimits":
        """The stricter of two limit sets, field by field."""
        if other is None:
            return self
        budgets = [b for b in (self.token_budget, other.token_budget) if b is not None]
        return DelegationLimits(
            max_depth=min(self.max_depth, other.max_depth),
            max_children=min(self.max_children, other.max_children),
            max_runs=min(self.max_runs, other.max_runs),
            token_budget=min(budgets) if budgets else None,
            wait_s=min(self.wait_s, other.wait_s),
        )


def child_run_id(parent_run_id: UUID, invocation_id: str) -> UUID:
    """The run id of the child a delegation invocation creates. Deterministic."""
    return uuid5(_CHILD_NAMESPACE, f"{parent_run_id}/{invocation_id}")


# --- report text --------------------------------------------------------------------------------

REPORT_KIND = "hollis.delegation.report"
_OPEN, _CLOSE = "<agent-report", "</agent-report>"
_REPORT = re.compile(
    r'<agent-report agent="(?P<agent>[^"]*)" run="(?P<run>[^"]*)">\n(?P<body>.*?)\n</agent-report>',
    re.S,
)
_NEUTRAL_HANDLE = re.compile(r"\((C-[0-9a-f]{8})\)")


def report_block(agent: str, run_id: UUID, body: str) -> str:
    """A child's answer framed so that it cannot end the frame or open another."""
    safe = body.replace(_CLOSE, "</agent-report-escaped>").replace(_OPEN, "<agent-report-escaped")
    name = agent.replace('"', "'")
    return f'<agent-report agent="{name}" run="{run_id}">\n{safe}\n{_CLOSE}'


def extract_reports(text: str) -> list[tuple[str, str, str]]:
    """(agent, run id, body) of every framed answer in a text, in order."""
    return [(m["agent"], m["run"], m["body"]) for m in _REPORT.finditer(text)]


def report_meta(text: str) -> dict[str, Any] | None:
    """The Hollis-observed facts of a delegation result (its first line), if it has them."""
    first = text.split("\n", 1)[0].strip()
    if not first.startswith("{"):
        return None
    try:
        data: object = json.loads(first)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    meta = cast(dict[str, Any], data)
    return meta if meta.get("kind") == REPORT_KIND else None


def restore_citations(body: str, handles: set[str]) -> str:
    """Turn neutralized handles back into citations, only for evidence that is present."""
    return _NEUTRAL_HANDLE.sub(
        lambda m: f"[{m.group(1)}]" if m.group(1) in handles else m.group(0), body
    )
