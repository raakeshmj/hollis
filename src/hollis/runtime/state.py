"""Run status and the legal transition table."""

from collections.abc import Mapping
from enum import StrEnum

from hollis.runtime.events import EventType


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
