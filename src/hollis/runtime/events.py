"""Run event payloads.

Every payload carries a literal `type` discriminator. Payload shapes are versioned per type;
see ADR-0005 for the evolution rules. M2 reset the pre-release baseline (ADR-0007): versions
below MIN_SUPPORTED_SCHEMA_VERSIONS were never released and are rejected.
"""

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal, Self, cast
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from hollis.runtime.model import (
    CallOutcome,
    DeploymentRef,
    FailureKind,
    ModelCallError,
    ModelResponse,
)
from hollis.runtime.policy import AmbiguousOutcomePolicy, RunBudget


class EventType(StrEnum):
    RUN_CREATED = "run_created"
    MODEL_CALL_STARTED = "model_call_started"
    MODEL_RESPONSE_RECORDED = "model_response_recorded"
    MODEL_CALL_FAILED = "model_call_failed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


class _Payload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunCreated(_Payload):
    type: Literal[EventType.RUN_CREATED] = EventType.RUN_CREATED
    model: str = Field(min_length=1)
    input: str
    instructions: str | None = None
    deployment: DeploymentRef
    budget: RunBudget
    ambiguous_policy: AmbiguousOutcomePolicy


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


class ProviderFailure(_Payload):
    """The recorded, already-sanitized form of a ModelCallError."""

    kind: FailureKind
    message: str
    provider_request_id: str | None = None
    retry_after_s: float | None = Field(default=None, ge=0)

    @property
    def outcome(self) -> CallOutcome:
        return self.kind.outcome

    @classmethod
    def from_error(cls, error: ModelCallError, *, message: str | None = None) -> "ProviderFailure":
        return cls(
            kind=error.kind,
            message=error.message if message is None else message,
            provider_request_id=error.provider_request_id,
            retry_after_s=error.retry_after_s,
        )


class ModelCallFailed(_Payload):
    """The recorded outcome of a physical attempt that produced no response."""

    type: Literal[EventType.MODEL_CALL_FAILED] = EventType.MODEL_CALL_FAILED
    operation_id: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    failure: ProviderFailure


class RunCompleted(_Payload):
    type: Literal[EventType.RUN_COMPLETED] = EventType.RUN_COMPLETED


class FailureReason(StrEnum):
    PROVIDER_ERROR = "provider_error"
    """The last attempt failed in a way retrying cannot fix."""
    AMBIGUOUS_OUTCOME = "ambiguous_outcome"
    """The last attempt's outcome is unknown and the policy forbids retrying."""
    BUDGET_EXHAUSTED = "budget_exhausted"
    """Attempts or the deadline ran out."""


class RunFailed(_Payload):
    type: Literal[EventType.RUN_FAILED] = EventType.RUN_FAILED
    operation_id: str = Field(min_length=1)
    reason: FailureReason
    message: str
    kind: FailureKind | None = None
    """Kind of the last provider failure, if any."""


AnyPayload = (
    RunCreated
    | ModelCallStarted
    | ModelResponseRecorded
    | ModelCallFailed
    | RunCompleted
    | RunFailed
)
EventPayload = Annotated[AnyPayload, Field(discriminator="type")]

# Current payload schema version per event type. Bump a version only together with an
# upcaster in _UPCASTERS that converts the previous version's payload dict.
CURRENT_SCHEMA_VERSIONS: Mapping[EventType, int] = {
    EventType.RUN_CREATED: 2,
    EventType.MODEL_CALL_STARTED: 1,
    EventType.MODEL_RESPONSE_RECORDED: 2,
    EventType.MODEL_CALL_FAILED: 1,
    EventType.RUN_COMPLETED: 1,
    EventType.RUN_FAILED: 2,
}

# Oldest version a reader accepts. Raised only for the pre-release baseline reset in M2:
# version-1 payloads of these types were produced by M1 development builds only.
MIN_SUPPORTED_SCHEMA_VERSIONS: Mapping[EventType, int] = {
    **dict.fromkeys(EventType, 1),
    EventType.RUN_CREATED: 2,
    EventType.MODEL_RESPONSE_RECORDED: 2,
    EventType.RUN_FAILED: 2,
}

type Upcaster = Callable[[dict[str, Any]], dict[str, Any]]

# (event type, from_version) -> function producing the payload dict at from_version + 1.
_UPCASTERS: Mapping[tuple[EventType, int], Upcaster] = {}


class EventEnvelope(BaseModel):
    """One immutable, ordered entry in a run's event log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: UUID
    run_id: UUID
    sequence: int = Field(ge=1)
    schema_version: int = Field(ge=1)
    occurred_at: AwareDatetime
    payload: EventPayload

    @property
    def event_type(self) -> EventType:
        return self.payload.type

    @field_validator("occurred_at")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        if value.utcoffset() != timedelta(0):
            raise ValueError("occurred_at must be UTC")
        return value

    @model_validator(mode="after")
    def _require_current_schema(self) -> Self:
        # In-memory envelopes always hold current-shape payloads; old versions are upcast on read.
        if self.schema_version != CURRENT_SCHEMA_VERSIONS[self.event_type]:
            raise ValueError(f"schema_version {self.schema_version} is not current")
        return self


def new_event(
    *,
    event_id: UUID,
    run_id: UUID,
    sequence: int,
    occurred_at: datetime,
    payload: AnyPayload,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        run_id=run_id,
        sequence=sequence,
        schema_version=CURRENT_SCHEMA_VERSIONS[payload.type],
        occurred_at=occurred_at,
        payload=payload,
    )


class EventDecodeError(ValueError):
    """Stored event data could not be decoded into an EventEnvelope."""


class UnknownEventSchema(EventDecodeError):
    """The event type or schema version is not known to this version of Hollis."""


def serialize_event(event: EventEnvelope) -> str:
    return event.model_dump_json()


def deserialize_event(raw: str | bytes) -> EventEnvelope:
    try:
        data: object = json.loads(raw)
    except ValueError as exc:
        raise EventDecodeError(f"invalid event JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise EventDecodeError("event JSON must be an object")
    return event_from_dict(cast(dict[str, Any], data))


def event_from_dict(data: Mapping[str, Any]) -> EventEnvelope:
    """Decode a stored event, upcasting older payload versions to the current schema."""
    payload: object = data.get("payload")
    version: object = data.get("schema_version")
    if not isinstance(payload, dict) or not isinstance(version, int):
        raise EventDecodeError("event is missing payload or schema_version")
    payload_dict = cast(dict[str, Any], payload)
    try:
        event_type = EventType(payload_dict.get("type"))
    except ValueError:
        raise UnknownEventSchema(f"unknown event type {payload_dict.get('type')!r}") from None
    current = CURRENT_SCHEMA_VERSIONS[event_type]
    if not MIN_SUPPORTED_SCHEMA_VERSIONS[event_type] <= version <= current:
        raise UnknownEventSchema(f"unsupported schema_version {version} for {event_type}")
    while version < current:
        payload_dict = _UPCASTERS[(event_type, version)](payload_dict)
        version += 1
    try:
        return EventEnvelope.model_validate(
            {**data, "payload": payload_dict, "schema_version": version}
        )
    except ValidationError as exc:
        raise EventDecodeError(str(exc)) from exc
