"""Run status, the legal transition table, and state reconstruction by folding events."""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

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
)
from hollis.runtime.model import CallOutcome, DeploymentRef, FailureKind, ModelResponse
from hollis.runtime.policy import AmbiguousOutcomePolicy, RunBudget


class RunStatus(StrEnum):
    PENDING = "pending"
    MODEL_CALL_IN_FLIGHT = "model_call_in_flight"
    """An attempt's intent is recorded and its outcome is not."""
    ATTEMPT_FAILED = "attempt_failed"
    """The latest attempt has a recorded failure; the runner decides whether to retry."""
    MODEL_RESPONDED = "model_responded"
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

# (current status, event type) -> next status. `None` means "no run yet".
# Anything absent from this table is illegal. Every attempt ends with a recorded outcome
# (response or failure) before another attempt may start.
TRANSITIONS: Mapping[tuple[RunStatus | None, EventType], RunStatus] = {
    (None, E.RUN_CREATED): S.PENDING,
    (S.PENDING, E.MODEL_CALL_STARTED): S.MODEL_CALL_IN_FLIGHT,
    (S.PENDING, E.RUN_FAILED): S.FAILED,
    (S.MODEL_CALL_IN_FLIGHT, E.MODEL_RESPONSE_RECORDED): S.MODEL_RESPONDED,
    (S.MODEL_CALL_IN_FLIGHT, E.MODEL_CALL_FAILED): S.ATTEMPT_FAILED,
    (S.ATTEMPT_FAILED, E.MODEL_CALL_STARTED): S.MODEL_CALL_IN_FLIGHT,
    (S.ATTEMPT_FAILED, E.RUN_FAILED): S.FAILED,
    (S.MODEL_RESPONDED, E.RUN_COMPLETED): S.COMPLETED,
}


def next_status(current: RunStatus | None, event_type: EventType) -> RunStatus:
    try:
        return TRANSITIONS[(current, event_type)]
    except KeyError:
        raise IllegalTransition(current, event_type) from None


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
    operation_id: str | None = None
    attempts: int = Field(default=0, ge=0)
    last_failure: ProviderFailure | None = None
    response: ModelResponse | None = None
    failure: RunFailure | None = None
    usage: UsageTotals = UsageTotals()

    @property
    def deadline(self) -> datetime | None:
        if self.budget.deadline_s is None:
            return None
        return self.created_at + timedelta(seconds=self.budget.deadline_s)


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
        )

    if state is None:  # unreachable: next_status only allows RunCreated without a run
        raise IllegalTransition(None, event.event_type)
    update: dict[str, Any] = {"version": event.sequence, "status": status}
    match payload:
        case ModelCallStarted(operation_id=op, attempt=attempt):
            if state.operation_id is not None and op != state.operation_id:
                raise InvalidEventSequence(f"operation_id {op!r} != {state.operation_id!r}")
            if attempt != state.attempts + 1:
                raise InvalidEventSequence(f"attempt {attempt} after {state.attempts}")
            if attempt > state.budget.max_attempts:
                raise InvalidEventSequence(
                    f"attempt {attempt} exceeds max_attempts {state.budget.max_attempts}"
                )
            update |= {"operation_id": op, "attempts": attempt}
        case ModelResponseRecorded(operation_id=op, attempt=attempt, response=response):
            _require_current_attempt(state, op, attempt)
            update["response"] = response
            if response.usage is not None:
                update["usage"] = UsageTotals(
                    input_tokens=state.usage.input_tokens + response.usage.input_tokens,
                    output_tokens=state.usage.output_tokens + response.usage.output_tokens,
                )
        case ModelCallFailed(operation_id=op, attempt=attempt, failure=failure):
            _require_current_attempt(state, op, attempt)
            update["last_failure"] = failure
        case RunFailed(operation_id=op, reason=reason, message=message, kind=kind):
            if state.operation_id is not None and op != state.operation_id:
                raise InvalidEventSequence(f"operation_id {op!r} != {state.operation_id!r}")
            _require_consistent_reason(state, reason)
            update["failure"] = RunFailure(reason=reason, message=message, kind=kind)
        case RunCompleted():
            pass
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


def _require_current_attempt(state: RunState, operation_id: str, attempt: int) -> None:
    if operation_id != state.operation_id:
        raise InvalidEventSequence(f"operation_id {operation_id!r} != {state.operation_id!r}")
    if attempt != state.attempts:
        raise InvalidEventSequence(f"outcome for attempt {attempt}, latest {state.attempts}")


def _require_consistent_reason(state: RunState, reason: FailureReason) -> None:
    failure = state.last_failure if state.status is RunStatus.ATTEMPT_FAILED else None
    match reason:
        case FailureReason.PROVIDER_ERROR:
            if failure is None or failure.kind.retryable:
                raise InvalidEventSequence("provider_error requires a non-retryable last failure")
        case FailureReason.AMBIGUOUS_OUTCOME:
            if failure is None or failure.outcome is not CallOutcome.AMBIGUOUS:
                raise InvalidEventSequence("ambiguous_outcome requires an ambiguous last failure")
        case FailureReason.BUDGET_EXHAUSTED:
            pass
