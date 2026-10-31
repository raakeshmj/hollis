"""Run status, the legal transition table, and state reconstruction by folding events."""

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hollis.runtime.events import (
    EventEnvelope,
    EventType,
    ModelCallStarted,
    ModelResponseRecorded,
    RunCompleted,
    RunCreated,
    RunFailed,
)
from hollis.runtime.model import ModelResponse


class RunStatus(StrEnum):
    PENDING = "pending"
    MODEL_CALL_IN_FLIGHT = "model_call_in_flight"
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


# (current status, event type) -> next status. `None` means "no run yet".
# Anything absent from this table is illegal.
TRANSITIONS: Mapping[tuple[RunStatus | None, EventType], RunStatus] = {
    (None, EventType.RUN_CREATED): RunStatus.PENDING,
    (RunStatus.PENDING, EventType.MODEL_CALL_STARTED): RunStatus.MODEL_CALL_IN_FLIGHT,
    (RunStatus.MODEL_CALL_IN_FLIGHT, EventType.MODEL_CALL_STARTED): RunStatus.MODEL_CALL_IN_FLIGHT,
    (RunStatus.MODEL_CALL_IN_FLIGHT, EventType.MODEL_RESPONSE_RECORDED): RunStatus.MODEL_RESPONDED,
    (RunStatus.MODEL_CALL_IN_FLIGHT, EventType.RUN_FAILED): RunStatus.FAILED,
    (RunStatus.MODEL_RESPONDED, EventType.RUN_COMPLETED): RunStatus.COMPLETED,
}


def next_status(current: RunStatus | None, event_type: EventType) -> RunStatus:
    try:
        return TRANSITIONS[(current, event_type)]
    except KeyError:
        raise IllegalTransition(current, event_type) from None


class Failure(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    error_type: str
    message: str


class RunState(BaseModel):
    """A run's state, derived entirely from its event log. Never mutated in place."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    version: int = Field(ge=1)
    status: RunStatus
    model: str
    input: str
    instructions: str | None
    operation_id: str | None = None
    attempts: int = Field(default=0, ge=0)
    response: ModelResponse | None = None
    failure: Failure | None = None


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
            model=payload.model,
            input=payload.input,
            instructions=payload.instructions,
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
            update |= {"operation_id": op, "attempts": attempt}
        case ModelResponseRecorded(operation_id=op, attempt=attempt, response=response):
            _require_operation(state, op)
            if attempt != state.attempts:
                raise InvalidEventSequence(
                    f"response for attempt {attempt}, latest {state.attempts}"
                )
            update["response"] = response
        case RunFailed(operation_id=op, error_type=error_type, message=message):
            _require_operation(state, op)
            update["failure"] = Failure(error_type=error_type, message=message)
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


def _require_operation(state: RunState, operation_id: str) -> None:
    if operation_id != state.operation_id:
        raise InvalidEventSequence(f"operation_id {operation_id!r} != {state.operation_id!r}")
