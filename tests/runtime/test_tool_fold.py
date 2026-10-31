"""The fold rejects tool lifecycles that violate authorization or recovery rules.

These logs are built by hand, bypassing the runner: the protections hold whatever code appends
events.
"""

from typing import Any
from uuid import uuid4

import pytest

from hollis.runtime.conversation import ToolCall
from hollis.runtime.events import (
    AnyPayload,
    ModelCallStarted,
    ModelResponseRecorded,
    RunCompleted,
    ToolApprovalResolved,
    ToolCallDecided,
    ToolExecutionStarted,
    ToolOutcome,
    ToolResultRecorded,
)
from hollis.runtime.model import ModelResponse
from hollis.runtime.policy import RunBudget
from hollis.runtime.state import InvalidEventSequence, RunStatus, fold, operation_id_for
from hollis.runtime.tools import Decision, ToolEffect, approval_binding, invocation_id_for
from tests.support.events import build_log, created
from tests.support.tools import FakeTool

RUN = uuid4()
WRITE = FakeTool("write", effect=ToolEffect.MUTATING).descriptor.ref
READ = FakeTool("read").descriptor.ref
ARGS: dict[str, Any] = {"path": "a"}


def _inv(index: int, turn: int = 0) -> str:
    return invocation_id_for(RUN, turn, index)


def _prefix(*calls: tuple[str, str], budget: RunBudget | None = None) -> list[AnyPayload]:
    op = operation_id_for(RUN, 0)
    tool_calls = tuple(ToolCall(call_id=cid, name=name, arguments=ARGS) for name, cid in calls)
    return [
        created(tools=(WRITE, READ), budget=budget or RunBudget(max_output_tokens=10)),
        ModelCallStarted(operation_id=op, attempt=1),
        ModelResponseRecorded(
            operation_id=op, attempt=1, response=ModelResponse(content="", tool_calls=tool_calls)
        ),
    ]


def _decided(index: int, name: str, decision: Decision, call_id: str = "c0") -> ToolCallDecided:
    return ToolCallDecided(
        invocation_id=_inv(index),
        turn=0,
        index=index,
        call_id=call_id,
        tool=name,
        decision=decision,
        reason="r",
        arguments=ARGS if decision is not Decision.INVALID else None,
    )


def _approval(
    index: int, *, approved: bool = True, binding: str | None = None
) -> ToolApprovalResolved:
    return ToolApprovalResolved(
        invocation_id=_inv(index),
        binding=binding or approval_binding(RUN, _inv(index), WRITE, ARGS),
        approved=approved,
        approver="alice",
    )


def _started(index: int, attempt: int = 1) -> ToolExecutionStarted:
    return ToolExecutionStarted(invocation_id=_inv(index), attempt=attempt)


def _result(index: int, outcome: ToolOutcome) -> ToolResultRecorded:
    return ToolResultRecorded(invocation_id=_inv(index), outcome=outcome)


def _fold(payloads: list[AnyPayload]) -> Any:
    return fold(build_log(RUN, payloads))


def test_a_valid_approved_lifecycle_folds() -> None:
    state = _fold(
        [
            *_prefix(("write", "c0")),
            _decided(0, "write", Decision.REQUIRE_APPROVAL),
            _approval(0),
            _started(0),
            _result(0, ToolOutcome.SUCCESS),
            ModelCallStarted(operation_id=operation_id_for(RUN, 1), attempt=1),
        ]
    )
    assert state.status is RunStatus.MODEL_CALL_IN_FLIGHT
    assert state.turns[0].invocations[0].approver == "alice"


@pytest.mark.parametrize(
    ("tail", "message"),
    [
        (
            [_decided(0, "write", Decision.REQUIRE_APPROVAL), _started(0)],
            "approval in the turn is pending",
        ),
        (
            [
                _decided(0, "write", Decision.REQUIRE_APPROVAL),
                _approval(0, approved=False),
                _started(0),
            ],
            "not authorized",
        ),
        ([_decided(0, "write", Decision.DENY), _started(0)], "not authorized"),
        ([_decided(0, "write", Decision.INVALID), _started(0)], "not authorized"),
        (
            [
                _decided(0, "write", Decision.REQUIRE_APPROVAL),
                _approval(0, binding="sha256:forged"),
            ],
            "binding",
        ),
        ([_decided(0, "write", Decision.ALLOW), _approval(0)], "not awaiting approval"),
    ],
    ids=[
        "unapproved",
        "refused",
        "denied",
        "invalid",
        "forged-binding",
        "approval-without-request",
    ],
)
def test_execution_requires_authorization(tail: list[AnyPayload], message: str) -> None:
    with pytest.raises(InvalidEventSequence, match=message):
        _fold([*_prefix(("write", "c0")), *tail])


def test_approval_binding_covers_the_exact_arguments() -> None:
    other_args = approval_binding(RUN, _inv(0), WRITE, {"path": "b"})
    with pytest.raises(InvalidEventSequence, match="binding"):
        _fold(
            [
                *_prefix(("write", "c0")),
                _decided(0, "write", Decision.REQUIRE_APPROVAL),
                _approval(0, binding=other_args),
            ]
        )


def test_nothing_runs_while_another_call_in_the_turn_awaits_approval() -> None:
    with pytest.raises(InvalidEventSequence, match="pending"):
        _fold(
            [
                *_prefix(("read", "c0"), ("write", "c1")),
                _decided(0, "read", Decision.ALLOW),
                _decided(1, "write", Decision.REQUIRE_APPROVAL, call_id="c1"),
                _started(0),
            ]
        )


def test_non_idempotent_tools_cannot_be_re_executed() -> None:
    allowed = [*_prefix(("write", "c0")), _decided(0, "write", Decision.ALLOW), _started(0)]
    with pytest.raises(InvalidEventSequence, match="idempotent"):
        _fold([*allowed, _started(0, attempt=2)])
    read = [*_prefix(("read", "c0")), _decided(0, "read", Decision.ALLOW), _started(0)]
    assert _fold([*read, _started(0, attempt=2)]).turns[0].invocations[0].attempts == 2


@pytest.mark.parametrize(
    ("tail", "message"),
    [
        ([_decided(0, "write", Decision.ALLOW), _result(0, ToolOutcome.SUCCESS)], "never started"),
        ([_decided(0, "write", Decision.ALLOW), _result(0, ToolOutcome.DENIED)], "denied result"),
        ([_decided(0, "write", Decision.DENY), _result(0, ToolOutcome.INVALID)], "invalid result"),
        (
            [
                _decided(0, "write", Decision.ALLOW),
                _started(0),
                _result(0, ToolOutcome.SUCCESS),
                _result(0, ToolOutcome.SUCCESS),
            ],
            "already has a result",
        ),
    ],
    ids=["success-without-start", "denied-but-allowed", "invalid-but-denied", "double-result"],
)
def test_results_must_match_what_happened(tail: list[AnyPayload], message: str) -> None:
    with pytest.raises(InvalidEventSequence, match=message):
        _fold([*_prefix(("write", "c0")), *tail])


def test_decisions_must_follow_the_model_calls_in_order() -> None:
    with pytest.raises(InvalidEventSequence, match="in order"):
        _fold(
            [
                *_prefix(("read", "c0"), ("write", "c1")),
                _decided(1, "write", Decision.ALLOW, call_id="c1"),
            ]
        )
    with pytest.raises(InvalidEventSequence, match="different tool"):
        _fold([*_prefix(("read", "c0")), _decided(0, "write", Decision.ALLOW)])


def test_authorizing_a_tool_outside_the_run_is_rejected() -> None:
    log = _prefix(("delete", "c0"))
    with pytest.raises(InvalidEventSequence, match="not enabled"):
        _fold([*log, _decided(0, "delete", Decision.ALLOW)])


def test_limits_are_enforced_by_the_fold() -> None:
    budget = RunBudget(max_output_tokens=10, max_tool_calls_per_turn=1)
    with pytest.raises(InvalidEventSequence, match="per_turn"):
        _fold(
            [
                *_prefix(("read", "c0"), ("read", "c1"), budget=budget),
                _decided(0, "read", Decision.ALLOW),
                _decided(1, "read", Decision.ALLOW, call_id="c1"),
            ]
        )


def test_next_turn_and_completion_require_every_tool_result() -> None:
    allowed = [*_prefix(("read", "c0")), _decided(0, "read", Decision.ALLOW), _started(0)]
    with pytest.raises(InvalidEventSequence, match="every tool call has a result"):
        _fold([*allowed, ModelCallStarted(operation_id=operation_id_for(RUN, 1), attempt=1)])
    with pytest.raises(InvalidEventSequence):
        _fold([*_prefix(("read", "c0")), RunCompleted()])
