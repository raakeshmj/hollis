import pytest
from pydantic import ValidationError

from hollis.runtime.events import EventType, ModelCallStarted, RunCreated
from hollis.runtime.model import ModelResponse, Usage


def test_payload_type_discriminator_is_fixed() -> None:
    assert RunCreated(model="m", input="hi").type is EventType.RUN_CREATED


def test_payloads_reject_undeclared_fields() -> None:
    with pytest.raises(ValidationError):
        RunCreated.model_validate({"model": "m", "input": "hi", "api_key": "secret"})


def test_payloads_are_immutable() -> None:
    payload = ModelCallStarted(operation_id="op", attempt=1)
    with pytest.raises(ValidationError):
        payload.attempt = 2  # type: ignore[misc]  # asserting frozen behaviour


def test_attempt_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        ModelCallStarted(operation_id="op", attempt=0)


def test_usage_rejects_negative_tokens() -> None:
    with pytest.raises(ValidationError):
        ModelResponse(content="x", usage=Usage(input_tokens=-1, output_tokens=0))
