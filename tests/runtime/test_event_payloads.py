import pytest
from pydantic import ValidationError

from hollis.runtime.events import EventType, ModelCallStarted, ProviderFailure, RunCreated
from hollis.runtime.model import (
    CallOutcome,
    FailureKind,
    ModelCallError,
    ModelResponse,
    Usage,
)
from hollis.runtime.policy import RunBudget
from tests.support.events import created


def test_payload_type_discriminator_is_fixed() -> None:
    assert created().type is EventType.RUN_CREATED


def test_payloads_reject_undeclared_fields() -> None:
    with pytest.raises(ValidationError):
        RunCreated.model_validate({**created().model_dump(), "api_key": "secret"})


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


@pytest.mark.parametrize(
    "budget",
    [
        {"max_output_tokens": 0},
        {"max_output_tokens": 1, "max_attempts": 0},
        {"max_output_tokens": 1, "attempt_timeout_s": 0},
        {"max_output_tokens": 1, "deadline_s": -1},
    ],
)
def test_budget_values_are_validated(budget: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        RunBudget.model_validate(budget)


@pytest.mark.parametrize("kind", list(FailureKind))
def test_failure_outcome_is_derived_from_kind(kind: FailureKind) -> None:
    error = ModelCallError(kind, "m", provider_request_id="req_1", retry_after_s=2.0)
    recorded = ProviderFailure.from_error(error)
    assert recorded.outcome is error.outcome is kind.outcome
    assert (recorded.provider_request_id, recorded.retry_after_s) == ("req_1", 2.0)


def test_ambiguous_kinds_are_exactly_those_that_may_have_generated() -> None:
    ambiguous = {k for k in FailureKind if k.outcome is CallOutcome.AMBIGUOUS}
    assert ambiguous == {
        FailureKind.TIMEOUT,
        FailureKind.SERVER_ERROR,
        FailureKind.CONNECTION_LOST,
        FailureKind.INTERRUPTED,
    }
    permanent = {k for k in FailureKind if not k.retryable}
    assert permanent == {
        FailureKind.AUTHENTICATION,
        FailureKind.PERMISSION,
        FailureKind.BILLING,
        FailureKind.INVALID_REQUEST,
        FailureKind.QUOTA_EXHAUSTED,
    }
