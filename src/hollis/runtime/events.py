"""Run event payloads.

Every payload carries a literal `type` discriminator. Payload shapes are versioned per type;
see `docs/plans/m1-runtime-core.md` §3 for the evolution rules.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hollis.runtime.model import ModelResponse


class EventType(StrEnum):
    RUN_CREATED = "run_created"
    MODEL_CALL_STARTED = "model_call_started"
    MODEL_RESPONSE_RECORDED = "model_response_recorded"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunCreated(_Payload):
    type: Literal[EventType.RUN_CREATED] = EventType.RUN_CREATED
    model: str = Field(min_length=1)
    input: str
    instructions: str | None = None


class ModelCallStarted(_Payload):
    """Intent to make one physical attempt of a logical model call. Appended before invoking."""

    type: Literal[EventType.MODEL_CALL_STARTED] = EventType.MODEL_CALL_STARTED
    operation_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)


class ModelResponseRecorded(_Payload):
    type: Literal[EventType.MODEL_RESPONSE_RECORDED] = EventType.MODEL_RESPONSE_RECORDED
    operation_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    response: ModelResponse


class RunCompleted(_Payload):
    type: Literal[EventType.RUN_COMPLETED] = EventType.RUN_COMPLETED


class RunFailed(_Payload):
    type: Literal[EventType.RUN_FAILED] = EventType.RUN_FAILED
    operation_id: str = Field(min_length=1)
    error_type: str
    message: str


type EventPayload = RunCreated | ModelCallStarted | ModelResponseRecorded | RunCompleted | RunFailed
