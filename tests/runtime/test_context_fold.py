"""The fold rejects context logs that would drop history silently or widen memory access.

Built by hand, bypassing the runner: the rules hold whatever code appends events.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from hollis.runtime.artifacts import artifact_ref
from hollis.runtime.context import (
    CompactionMethod,
    CompactionSummary,
    ContextBudget,
    ContextPolicy,
    SummaryItem,
    TokenAccounting,
    TokenEstimate,
)
from hollis.runtime.conversation import ToolCall
from hollis.runtime.events import (
    AnyPayload,
    ContextAssembled,
    ContextCompacted,
    FailureReason,
    MemoryRecalled,
    ModelCallStarted,
    ModelResponseRecorded,
    RunFailed,
    ToolCallDecided,
    ToolExecutionStarted,
    ToolOutcome,
    ToolResultRecorded,
)
from hollis.runtime.memory import Certainty, MemoryAccess, MemoryKind, RecalledMemory
from hollis.runtime.model import ModelResponse
from hollis.runtime.policy import RunBudget
from hollis.runtime.state import InvalidEventSequence, RunStatus, fold, operation_id_for
from hollis.runtime.tools import Decision, invocation_id_for
from tests.support.events import build_log, created
from tests.support.tools import FakeTool

RUN = uuid4()
READ = FakeTool("read").descriptor.ref
ESTIMATE = TokenEstimate(input_tokens=10, accounting=TokenAccounting.ESTIMATED, heuristic_tokens=10)
BUDGET = ContextBudget(
    window_tokens=None, reserved_output_tokens=10, margin_tokens=0, max_input_tokens=None
)
ACCESS = MemoryAccess(read=("user:alice", "agent:a"), write=("user:alice",))


def _memory(memory_id: str = "m1", scope: str = "user:alice") -> RecalledMemory:
    return RecalledMemory(
        memory_id=memory_id,
        scope=scope,
        kind=MemoryKind.PREFERENCE,
        content="prefers metric units",
        certainty=Certainty.CONFIRMED,
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
        source="user",
    )


def _assembled(turn: int, **overrides: Any) -> ContextAssembled:
    fields: dict[str, Any] = {
        "operation_id": operation_id_for(RUN, turn),
        "history_start": 0,
        "estimate": ESTIMATE,
        "budget": BUDGET,
    }
    return ContextAssembled(**(fields | overrides))


def _turn(turn: int, *, artifact: bool = False) -> list[AnyPayload]:
    op = operation_id_for(RUN, turn)
    inv = invocation_id_for(RUN, turn, 0)
    return [
        ModelCallStarted(operation_id=op, attempt=1),
        ModelResponseRecorded(
            operation_id=op,
            attempt=1,
            response=ModelResponse(
                content=f"turn {turn}",
                tool_calls=(ToolCall(call_id=f"c{turn}", name="read", arguments={}),),
            ),
        ),
        ToolCallDecided(
            invocation_id=inv,
            turn=turn,
            index=0,
            call_id=f"c{turn}",
            tool="read",
            decision=Decision.ALLOW,
            reason="ok",
            arguments={},
        ),
        ToolExecutionStarted(invocation_id=inv, attempt=1),
        ToolResultRecorded(
            invocation_id=inv,
            outcome=ToolOutcome.SUCCESS,
            artifact=artifact_ref("x" * 5000) if artifact else None,
        ),
    ]


def _summary(through: int, items: tuple[SummaryItem, ...] = (), **kw: Any) -> CompactionSummary:
    return CompactionSummary(
        through_turn=through, items=items, method=CompactionMethod.DETERMINISTIC, **kw
    )


def _item(n: int) -> SummaryItem:
    return SummaryItem(item_id=f"observation-{n}", text=f"note {n}", first_turn=0)


def _log(*payloads: AnyPayload, **created_overrides: Any) -> list[AnyPayload]:
    fields: dict[str, Any] = {"tools": (READ,), "budget": RunBudget(max_output_tokens=10)}
    return [created(**(fields | created_overrides)), *payloads]


def _fold(payloads: list[AnyPayload]) -> Any:
    return fold(build_log(RUN, payloads))


def test_valid_context_lifecycle_folds() -> None:
    state = _fold(
        _log(
            MemoryRecalled(query="q", scopes=ACCESS.read, memories=(_memory(),)),
            _assembled(0, memories=("m1",)),
            *_turn(0, artifact=True),
            _assembled(1),
            *_turn(1),
            ContextCompacted(compaction=0, summary=_summary(1, (_item(1),))),
            _assembled(2, history_start=1),
            memory=ACCESS,
        )
    )
    assert state.status is RunStatus.TOOL_CALLS
    assert state.compacted_through == 1
    assert state.memories[0].memory_id == "m1"
    assert state.turns[0].estimate == ESTIMATE


@pytest.mark.parametrize(
    ("payloads", "message"),
    [
        (
            [
                *_turn(0),
                *_turn(1),
                *_turn(2),
                ContextCompacted(compaction=0, summary=_summary(1)),
                _assembled(3),
            ],
            "history must start at turn 1",
        ),
        (
            [*_turn(0), *_turn(1), _assembled(2, history_start=1)],
            "history must start at turn 0",
        ),
        (
            [*_turn(0), *_turn(1), ContextCompacted(compaction=0, summary=_summary(2))],
            "keep the latest one",
        ),
        (
            [
                *_turn(0),
                *_turn(1),
                *_turn(2),
                ContextCompacted(compaction=0, summary=_summary(1, (_item(1), _item(2)))),
                ContextCompacted(compaction=1, summary=_summary(2, (_item(1),))),
            ],
            "drops items without resolving",
        ),
        (
            [*_turn(0), *_turn(1), _assembled(2, cleared=(invocation_id_for(RUN, 0, 0),))],
            "artifact-backed",
        ),
        ([_assembled(0, memories=("m-unknown",))], "not recalled"),
        ([_assembled(0), _assembled(0)], "already assembled"),
        ([_assembled(1)], "expected"),
    ],
    ids=[
        "skips-uncompacted-turns",
        "starts-without-summary",
        "compacts-latest-turn",
        "silently-drops-note",
        "clears-non-artifact-result",
        "includes-unrecalled-memory",
        "assembles-twice",
        "wrong-operation",
    ],
)
def test_illegal_context_logs_are_rejected(payloads: list[AnyPayload], message: str) -> None:
    with pytest.raises(InvalidEventSequence, match=message):
        _fold(_log(*payloads))


def test_recall_from_an_ungranted_scope_is_rejected() -> None:
    with pytest.raises(InvalidEventSequence, match="not granted"):
        _fold(_log(MemoryRecalled(query="q", scopes=("user:bob",)), memory=ACCESS))
    with pytest.raises(InvalidEventSequence, match="outside the recalled scopes"):
        _fold(
            _log(
                MemoryRecalled(
                    query="q", scopes=("user:alice",), memories=(_memory(scope="agent:a"),)
                ),
                memory=ACCESS,
            )
        )
    with pytest.raises(InvalidEventSequence, match="not granted"):
        _fold(_log(MemoryRecalled(query="q", scopes=("user:alice",))))  # no grant at all


def test_recall_happens_once_and_before_the_first_model_call() -> None:
    recall = MemoryRecalled(query="q", scopes=("user:alice",))
    with pytest.raises(InvalidEventSequence, match="once"):
        _fold(_log(recall, recall, memory=ACCESS))
    with pytest.raises(InvalidEventSequence, match="once"):
        _fold(_log(_assembled(0), recall, memory=ACCESS))


def test_compaction_respects_the_policy_limit() -> None:
    with pytest.raises(InvalidEventSequence, match="max_compactions"):
        _fold(
            _log(
                *_turn(0),
                *_turn(1),
                ContextCompacted(compaction=0, summary=_summary(1)),
                context=ContextPolicy(max_compactions=0),
            )
        )


def test_context_exhausted_is_only_valid_before_a_model_call() -> None:
    failed = RunFailed(
        operation_id=operation_id_for(RUN, 0), reason=FailureReason.CONTEXT_EXHAUSTED, message="m"
    )
    assert _fold(_log(failed)).status is RunStatus.FAILED
    responded = _turn(0)[:2]  # a response with an unanswered tool call
    with pytest.raises(InvalidEventSequence, match="context_exhausted"):
        _fold(_log(*responded, failed))
