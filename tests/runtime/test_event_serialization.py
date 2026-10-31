import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from hypothesis import given
from pydantic import ValidationError

from hollis.runtime.events import (
    CURRENT_SCHEMA_VERSIONS,
    EventDecodeError,
    EventEnvelope,
    EventType,
    UnknownEventSchema,
    deserialize_event,
    serialize_event,
)
from tests.support.events import T0, build_log, created, happy_payloads, valid_logs


@given(valid_logs())
def test_round_trip_preserves_every_event(log: list[EventEnvelope]) -> None:
    for event in log:
        assert deserialize_event(serialize_event(event)) == event


def test_serialized_form_is_versioned_json() -> None:
    event = build_log(uuid4(), [created()])[0]
    data = json.loads(serialize_event(event))
    assert set(data) == {
        "event_id",
        "run_id",
        "sequence",
        "schema_version",
        "occurred_at",
        "payload",
    }
    assert data["schema_version"] == 2
    assert data["payload"]["type"] == "run_created"


def test_event_type_is_derived_from_payload() -> None:
    run = uuid4()
    types = [e.event_type for e in build_log(run, happy_payloads(run))]
    assert types == [
        EventType.RUN_CREATED,
        EventType.MODEL_CALL_STARTED,
        EventType.MODEL_RESPONSE_RECORDED,
        EventType.RUN_COMPLETED,
    ]


def _raw(**overrides: object) -> str:
    data = json.loads(serialize_event(build_log(uuid4(), [created()])[0]))
    for key, value in overrides.items():
        if key == "payload_type":
            data["payload"]["type"] = value
        else:
            data[key] = value
    return json.dumps(data)


def test_unknown_event_type_is_rejected() -> None:
    with pytest.raises(UnknownEventSchema, match="unknown event type"):
        deserialize_event(_raw(payload_type="run_teleported"))


@pytest.mark.parametrize(
    "version",
    [0, 1, CURRENT_SCHEMA_VERSIONS[EventType.RUN_CREATED] + 1],
    ids=["zero", "pre-m2-baseline", "future"],
)
def test_unsupported_schema_version_is_rejected(version: int) -> None:
    with pytest.raises(UnknownEventSchema, match="unsupported schema_version"):
        deserialize_event(_raw(schema_version=version))


@pytest.mark.parametrize(
    "raw",
    ["not json", "[]", '{"payload": {"type": "run_created"}}', _raw(sequence=0)],
    ids=["invalid-json", "not-object", "no-version", "bad-envelope"],
)
def test_malformed_events_raise_decode_error(raw: str) -> None:
    with pytest.raises(EventDecodeError):
        deserialize_event(raw)


def test_envelope_requires_utc_timestamp() -> None:
    event = build_log(uuid4(), [created()])[0]
    data = event.model_dump()
    data["occurred_at"] = T0.astimezone(timezone(timedelta(hours=5, minutes=30)))
    with pytest.raises(ValidationError, match="UTC"):
        EventEnvelope.model_validate(data)
    data["occurred_at"] = datetime(2026, 1, 1)  # deliberately naive
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(data)


def test_envelope_rejects_non_current_schema_version_in_memory() -> None:
    event = build_log(uuid4(), [created()])[0]
    with pytest.raises(ValidationError, match="not current"):
        EventEnvelope.model_validate({**event.model_dump(), "schema_version": 3})
