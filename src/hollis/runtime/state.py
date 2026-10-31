"""Run status, the legal transition table, and state reconstruction by folding events.

The fold is the enforcement point for run semantics (ADR-0005, ADR-0007, ADR-0014): whatever code
appends events, a log that violates these rules cannot be folded. In particular, a tool can only
start executing after an allow decision or a matching recorded approval, nothing in a turn runs
while any approval in it is pending, and a started invocation can only be retried if its tool is
declared idempotent.
"""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from hollis.runtime.conversation import ToolCall
from hollis.runtime.events import (
    EventEnvelope,
    EventType,
    FailureReason,
    ModelCallFailed,
    ModelCallStarted,
    ModelResponseRecorded,
    ProviderFailure,
    RunCompleted,
    RunCreated,
    RunFailed,
    ToolApprovalResolved,
    ToolCallDecided,
    ToolExecutionStarted,
    ToolOutcome,
    ToolResultRecorded,
)
from hollis.runtime.model import CallOutcome, DeploymentRef, FailureKind, ModelResponse
from hollis.runtime.policy import AmbiguousOutcomePolicy, RunBudget
from hollis.runtime.tools import (
    Decision,
    ResultPart,
    ToolRef,
    approval_binding,
    invocation_id_for,
)


class RunStatus(StrEnum):
    PENDING = "pending"
    MODEL_CALL_IN_FLIGHT = "model_call_in_flight"
    """An attempt's intent is recorded and its outcome is not."""
    ATTEMPT_FAILED = "attempt_failed"
    """The latest attempt has a recorded failure; the runner decides whether to retry."""
    MODEL_RESPONDED = "model_responded"
    TOOL_CALLS = "tool_calls"
    """The latest response's tool calls are being decided, approved or executed."""
    COMPLETED = "completed"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (RunStatus.COMPLETED, RunStatus.FAILED)


class InvalidEventSequence(Exception):
    """An event log (or a single appended event) violates run semantics."""


class IllegalTransition(InvalidEventSequence):
    def __init__(self, current: RunStatus | None, event_type: EventType) -> None:
        self.current = current
        self.event_type = event_type
        super().__init__(f"illegal transition: {event_type} in status {current}")


S = RunStatus
E = EventType
_TOOL_EVENTS = (
    E.TOOL_CALL_DECIDED,
    E.TOOL_APPROVAL_RESOLVED,
    E.TOOL_EXECUTION_STARTED,
    E.TOOL_RESULT_RECORDED,
)

# (current status, event type) -> next status. `None` means "no run yet". Anything absent is
# illegal. Finer rules (which invocation, which turn) are checked in `apply`.
TRANSITIONS: Mapping[tuple[RunStatus | None, EventType], RunStatus] = {
    (None, E.RUN_CREATED): S.PENDING,
    (S.PENDING, E.MODEL_CALL_STARTED): S.MODEL_CALL_IN_FLIGHT,
    (S.PENDING, E.RUN_FAILED): S.FAILED,
    (S.MODEL_CALL_IN_FLIGHT, E.MODEL_RESPONSE_RECORDED): S.MODEL_RESPONDED,
    (S.MODEL_CALL_IN_FLIGHT, E.MODEL_CALL_FAILED): S.ATTEMPT_FAILED,
    (S.ATTEMPT_FAILED, E.MODEL_CALL_STARTED): S.MODEL_CALL_IN_FLIGHT,
    (S.ATTEMPT_FAILED, E.RUN_FAILED): S.FAILED,
    (S.MODEL_RESPONDED, E.RUN_COMPLETED): S.COMPLETED,
    (S.MODEL_RESPONDED, E.TOOL_CALL_DECIDED): S.TOOL_CALLS,
    (S.MODEL_RESPONDED, E.RUN_FAILED): S.FAILED,
    **{(S.TOOL_CALLS, event): S.TOOL_CALLS for event in _TOOL_EVENTS},
    (S.TOOL_CALLS, E.MODEL_CALL_STARTED): S.MODEL_CALL_IN_FLIGHT,
    (S.TOOL_CALLS, E.RUN_FAILED): S.FAILED,
}


def next_status(current: RunStatus | None, event_type: EventType) -> RunStatus:
    try:
        return TRANSITIONS[(current, event_type)]
    except KeyError:
        raise IllegalTransition(current, event_type) from None


def operation_id_for(run_id: UUID, call_index: int = 0) -> str:
    """Stable idempotency key for a run's logical model call (one per turn)."""
    return f"{run_id}/model/{call_index}"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunFailure(_Frozen):
    reason: FailureReason
    message: str
    kind: FailureKind | None = None


class UsageTotals(_Frozen):
    """Provider-reported usage summed over every recorded response of the run."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class Invocation(_Frozen):
    """One model tool call and everything decided and done about it."""

    invocation_id: str
    index: int
    call: ToolCall
    decision: Decision | None = None
    reason: str | None = None
    rule: str | None = None
    arguments: dict[str, JsonValue] | None = None
    decided_at: AwareDatetime | None = None
    approved: bool | None = None
    approver: str | None = None
    approval_reason: str | None = None
    attempts: int = 0
    outcome: ToolOutcome | None = None
    parts: tuple[ResultPart, ...] = ()
    truncated: bool = False
    original_chars: int = 0

    @property
    def awaiting_approval(self) -> bool:
        return self.decision is Decision.REQUIRE_APPROVAL and self.approved is None

    @property
    def authorized(self) -> bool:
        return self.decision is Decision.ALLOW or (
            self.decision is Decision.REQUIRE_APPROVAL and self.approved is True
        )

    @property
    def finished(self) -> bool:
        return self.outcome is not None


class Turn(_Frozen):
    response: ModelResponse
    invocations: tuple[Invocation, ...] = ()


class RunState(_Frozen):
    """A run's state, derived entirely from its event log. Never mutated in place."""

    run_id: UUID
    version: int = Field(ge=1)
    status: RunStatus
    created_at: AwareDatetime
    model: str
    input: str
    instructions: str | None
    deployment: DeploymentRef
    budget: RunBudget
    ambiguous_policy: AmbiguousOutcomePolicy
    tools: tuple[ToolRef, ...] = ()
    turns: tuple[Turn, ...] = ()
    operation_id: str | None = None
    attempts: int = Field(default=0, ge=0)
    """Attempts of the current logical model call."""
    last_failure: ProviderFailure | None = None
    response: ModelResponse | None = None
    """The latest recorded model response."""
    failure: RunFailure | None = None
    usage: UsageTotals = UsageTotals()
    tool_calls_authorized: int = 0
    """Invocations decided as allow or require-approval (counted against the budget)."""

    @property
    def deadline(self) -> datetime | None:
        if self.budget.deadline_s is None:
            return None
        return self.created_at + timedelta(seconds=self.budget.deadline_s)

    def tool(self, name: str) -> ToolRef | None:
        return next((t for t in self.tools if t.name == name), None)

    @property
    def pending_invocations(self) -> tuple[Invocation, ...]:
        """Invocations of the latest turn that have no result yet."""
        if not self.turns:
            return ()
        return tuple(i for i in self.turns[-1].invocations if not i.finished)

    @property
    def awaiting_approval(self) -> tuple[Invocation, ...]:
        return tuple(i for i in self.pending_invocations if i.awaiting_approval)


def apply(state: RunState | None, event: EventEnvelope) -> RunState:
    """Return the state after `event`, or raise InvalidEventSequence. Pure."""
    expected_sequence = 1 if state is None else state.version + 1
    if event.sequence != expected_sequence:
        raise InvalidEventSequence(f"expected sequence {expected_sequence}, got {event.sequence}")
    if state is not None and event.run_id != state.run_id:
        raise InvalidEventSequence(f"event for run {event.run_id} in log of run {state.run_id}")

    status = next_status(state.status if state else None, event.event_type)
    payload = event.payload
    if isinstance(payload, RunCreated):
        names = [t.name for t in payload.tools]
        if len(set(names)) != len(names):
            raise InvalidEventSequence("duplicate tool names in RunCreated")
        return RunState(
            run_id=event.run_id,
            version=event.sequence,
            status=status,
            created_at=event.occurred_at,
            model=payload.model,
            input=payload.input,
            instructions=payload.instructions,
            deployment=payload.deployment,
            budget=payload.budget,
            ambiguous_policy=payload.ambiguous_policy,
            tools=payload.tools,
        )

    if state is None:  # unreachable: next_status only allows RunCreated without a run
        raise IllegalTransition(None, event.event_type)
    update: dict[str, Any] = {"version": event.sequence, "status": status}
    match payload:
        case ModelCallStarted(operation_id=op, attempt=attempt):
            update |= _model_call_started(state, op, attempt)
        case ModelResponseRecorded(operation_id=op, attempt=attempt, response=response):
            _require_current_attempt(state, op, attempt)
            turn_index = len(state.turns)
            invocations = tuple(
                Invocation(
                    invocation_id=invocation_id_for(state.run_id, turn_index, i),
                    index=i,
                    call=call,
                )
                for i, call in enumerate(response.tool_calls)
            )
            update |= {
                "response": response,
                "turns": (*state.turns, Turn(response=response, invocations=invocations)),
            }
            if response.usage is not None:
                update["usage"] = UsageTotals(
                    input_tokens=state.usage.input_tokens + response.usage.input_tokens,
                    output_tokens=state.usage.output_tokens + response.usage.output_tokens,
                )
        case ModelCallFailed(operation_id=op, attempt=attempt, failure=failure):
            _require_current_attempt(state, op, attempt)
            update["last_failure"] = failure
        case ToolCallDecided():
            update |= _tool_call_decided(state, payload, event.occurred_at)
        case ToolApprovalResolved():
            update |= _tool_approval_resolved(state, payload)
        case ToolExecutionStarted():
            update |= _tool_execution_started(state, payload)
        case ToolResultRecorded():
            update |= _tool_result_recorded(state, payload)
        case RunFailed(operation_id=op, reason=reason, message=message, kind=kind):
            if state.operation_id is not None and op != state.operation_id:
                raise InvalidEventSequence(f"operation_id {op!r} != {state.operation_id!r}")
            _require_consistent_reason(state, reason)
            update["failure"] = RunFailure(reason=reason, message=message, kind=kind)
        case RunCompleted():
            if state.response is not None and state.response.tool_calls:
                raise InvalidEventSequence("cannot complete with unanswered tool calls")
    return state.model_copy(update=update)


def fold(events: Iterable[EventEnvelope]) -> RunState:
    """Reconstruct a run's state from its complete event log. Pure."""
    state: RunState | None = None
    seen: set[UUID] = set()
    for event in events:
        if event.event_id in seen:
            raise InvalidEventSequence(f"duplicate event_id {event.event_id}")
        seen.add(event.event_id)
        state = apply(state, event)
    if state is None:
        raise InvalidEventSequence("empty event log")
    return state


# --- model calls --------------------------------------------------------------------------------


def _model_call_started(state: RunState, op: str, attempt: int) -> dict[str, Any]:
    if state.status is RunStatus.ATTEMPT_FAILED:
        if op != state.operation_id:
            raise InvalidEventSequence(f"operation_id {op!r} != {state.operation_id!r}")
        if attempt != state.attempts + 1:
            raise InvalidEventSequence(f"attempt {attempt} after {state.attempts}")
        if attempt > state.budget.max_attempts:
            raise InvalidEventSequence(
                f"attempt {attempt} exceeds max_attempts {state.budget.max_attempts}"
            )
        return {"attempts": attempt}
    # A new logical call: the run's first, or the next turn after all tool results are in.
    turn = len(state.turns)
    if state.status is RunStatus.TOOL_CALLS and state.pending_invocations:
        raise InvalidEventSequence("next model turn before every tool call has a result")
    if turn >= state.budget.max_model_turns:
        raise InvalidEventSequence(f"turn {turn} exceeds max_model_turns")
    expected = operation_id_for(state.run_id, turn)
    if op != expected:
        raise InvalidEventSequence(f"operation_id {op!r} != {expected!r}")
    if attempt != 1:
        raise InvalidEventSequence(f"a new model call starts at attempt 1, not {attempt}")
    return {"operation_id": op, "attempts": 1, "last_failure": None}


def _require_current_attempt(state: RunState, operation_id: str, attempt: int) -> None:
    if operation_id != state.operation_id:
        raise InvalidEventSequence(f"operation_id {operation_id!r} != {state.operation_id!r}")
    if attempt != state.attempts:
        raise InvalidEventSequence(f"outcome for attempt {attempt}, latest {state.attempts}")


def _require_consistent_reason(state: RunState, reason: FailureReason) -> None:
    if state.status in (RunStatus.MODEL_RESPONDED, RunStatus.TOOL_CALLS):
        if reason not in (FailureReason.BUDGET_EXHAUSTED, FailureReason.MODEL_PROTOCOL_ERROR):
            raise InvalidEventSequence(f"{reason} is not a valid failure during tool calls")
        return
    failure = state.last_failure if state.status is RunStatus.ATTEMPT_FAILED else None
    match reason:
        case FailureReason.PROVIDER_ERROR:
            if failure is None or failure.kind.retryable:
                raise InvalidEventSequence("provider_error requires a non-retryable last failure")
        case FailureReason.AMBIGUOUS_OUTCOME:
            if failure is None or failure.outcome is not CallOutcome.AMBIGUOUS:
                raise InvalidEventSequence("ambiguous_outcome requires an ambiguous last failure")
        case FailureReason.MODEL_PROTOCOL_ERROR:
            raise InvalidEventSequence("model_protocol_error requires a recorded response")
        case FailureReason.BUDGET_EXHAUSTED:
            pass


# --- tool calls ---------------------------------------------------------------------------------


def _replace(state: RunState, invocation: Invocation) -> dict[str, Any]:
    last = state.turns[-1]
    invocations = tuple(
        invocation if i.invocation_id == invocation.invocation_id else i for i in last.invocations
    )
    return {"turns": (*state.turns[:-1], last.model_copy(update={"invocations": invocations}))}


def _invocation(state: RunState, invocation_id: str) -> Invocation:
    if state.turns:
        for invocation in state.turns[-1].invocations:
            if invocation.invocation_id == invocation_id:
                return invocation
    raise InvalidEventSequence(f"no tool call {invocation_id!r} in the current turn")


def _tool_call_decided(
    state: RunState, event: ToolCallDecided, occurred_at: datetime
) -> dict[str, Any]:
    if not state.turns or event.turn != len(state.turns) - 1:
        raise InvalidEventSequence(f"decision for turn {event.turn} outside the current turn")
    invocations = state.turns[-1].invocations
    undecided = [i for i in invocations if i.decision is None]
    if not undecided or undecided[0].index != event.index:
        raise InvalidEventSequence(f"tool calls must be decided in order (got {event.index})")
    invocation = undecided[0]
    if event.invocation_id != invocation.invocation_id or event.call_id != invocation.call.call_id:
        raise InvalidEventSequence("decision does not match the model's tool call")
    if event.tool != invocation.call.name:
        raise InvalidEventSequence("decision names a different tool than the model called")
    update: dict[str, Any] = {}
    if event.decision in (Decision.ALLOW, Decision.REQUIRE_APPROVAL):
        if state.tool(event.tool) is None:
            raise InvalidEventSequence(f"tool {event.tool!r} is not enabled for this run")
        if event.arguments is None:
            raise InvalidEventSequence("an authorized call needs validated arguments")
        this_turn = sum(
            1 for i in invocations if i.decision in (Decision.ALLOW, Decision.REQUIRE_APPROVAL)
        )
        if state.tool_calls_authorized >= state.budget.max_tool_calls:
            raise InvalidEventSequence("max_tool_calls exceeded")
        if this_turn >= state.budget.max_tool_calls_per_turn:
            raise InvalidEventSequence("max_tool_calls_per_turn exceeded")
        update["tool_calls_authorized"] = state.tool_calls_authorized + 1
    decided = invocation.model_copy(
        update={
            "decision": event.decision,
            "reason": event.reason,
            "rule": event.rule,
            "arguments": event.arguments,
            "decided_at": occurred_at,
        }
    )
    return update | _replace(state, decided)


def _tool_approval_resolved(state: RunState, event: ToolApprovalResolved) -> dict[str, Any]:
    invocation = _invocation(state, event.invocation_id)
    if not invocation.awaiting_approval:
        raise InvalidEventSequence(f"{event.invocation_id!r} is not awaiting approval")
    tool = state.tool(invocation.call.name)
    if tool is None:  # unreachable: REQUIRE_APPROVAL requires an enabled tool
        raise InvalidEventSequence("approval for a tool not enabled in this run")
    expected = approval_binding(state.run_id, invocation.invocation_id, tool, invocation.arguments)
    if event.binding != expected:
        raise InvalidEventSequence("approval binding does not match the pending invocation")
    resolved = invocation.model_copy(
        update={
            "approved": event.approved,
            "approver": event.approver,
            "approval_reason": event.reason,
        }
    )
    return _replace(state, resolved)


def _tool_execution_started(state: RunState, event: ToolExecutionStarted) -> dict[str, Any]:
    invocation = _invocation(state, event.invocation_id)
    turn = state.turns[-1].invocations
    if any(i.decision is None for i in turn):
        raise InvalidEventSequence("execution before every tool call in the turn is decided")
    if any(i.awaiting_approval for i in turn):
        raise InvalidEventSequence("execution while an approval in the turn is pending")
    if not invocation.authorized:
        raise InvalidEventSequence(f"{event.invocation_id!r} is not authorized")
    if invocation.finished:
        raise InvalidEventSequence(f"{event.invocation_id!r} already has a result")
    if event.attempt != invocation.attempts + 1:
        raise InvalidEventSequence(f"attempt {event.attempt} after {invocation.attempts}")
    tool = state.tool(invocation.call.name)
    if event.attempt > 1 and (tool is None or not tool.idempotent):
        raise InvalidEventSequence("re-execution requires an idempotent tool")
    return _replace(state, invocation.model_copy(update={"attempts": event.attempt}))


_EXECUTED = frozenset(
    {ToolOutcome.SUCCESS, ToolOutcome.ERROR, ToolOutcome.TIMEOUT, ToolOutcome.AMBIGUOUS}
)


def _tool_result_recorded(state: RunState, event: ToolResultRecorded) -> dict[str, Any]:
    invocation = _invocation(state, event.invocation_id)
    if invocation.finished:
        raise InvalidEventSequence(f"{event.invocation_id!r} already has a result")
    if invocation.decision is None:
        raise InvalidEventSequence("result for an undecided tool call")
    outcome = event.outcome
    if outcome in _EXECUTED and invocation.attempts == 0:
        raise InvalidEventSequence(f"{outcome} result for a tool call that never started")
    if outcome is ToolOutcome.INVALID and invocation.decision is not Decision.INVALID:
        raise InvalidEventSequence("invalid result for a call that was not rejected as invalid")
    if outcome is ToolOutcome.DENIED and not (
        invocation.decision is Decision.DENY or invocation.approved is False
    ):
        raise InvalidEventSequence("denied result without a denial or refused approval")
    if outcome in (ToolOutcome.INVALID, ToolOutcome.DENIED) and invocation.attempts:
        raise InvalidEventSequence("a started call cannot be recorded as invalid or denied")
    finished = invocation.model_copy(
        update={
            "outcome": outcome,
            "parts": event.parts,
            "truncated": event.truncated,
            "original_chars": event.original_chars,
        }
    )
    return _replace(state, finished)
