"""Deterministic builders and Hypothesis strategies for run event logs."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from hypothesis import strategies as st

from hollis.adapters.fake_provider import FAKE_DESCRIPTOR
from hollis.runtime.events import (
    AnyPayload,
    EventEnvelope,
    FailureReason,
    ModelCallFailed,
    ModelCallStarted,
    ModelResponseRecorded,
    ProviderFailure,
    RunCompleted,
    RunCreated,
    RunFailed,
    new_event,
)
from hollis.runtime.model import CallOutcome, FailureKind, ModelResponse, StopReason, Usage
from hollis.runtime.policy import AmbiguousOutcomePolicy, RunBudget
from hollis.runtime.runner import operation_id_for

T0 = datetime(2026, 1, 1, tzinfo=UTC)
BUDGET = RunBudget(max_output_tokens=256)


def created(**overrides: Any) -> RunCreated:
    fields: dict[str, Any] = {
        "model": "fake",
        "input": "hello",
        "deployment": FAKE_DESCRIPTOR.ref,
        "budget": BUDGET,
        "ambiguous_policy": AmbiguousOutcomePolicy.RETRY,
    }
    return RunCreated(**(fields | overrides))


def failure(kind: FailureKind = FailureKind.OVERLOADED, message: str = "boom") -> ProviderFailure:
    return ProviderFailure(kind=kind, message=message)


def build_log(
    run_id: UUID, payloads: Sequence[AnyPayload], *, start_sequence: int = 1
) -> list[EventEnvelope]:
    return [
        new_event(
            event_id=uuid4(),
            run_id=run_id,
            sequence=start_sequence + i,
            occurred_at=T0,
            payload=payload,
        )
        for i, payload in enumerate(payloads)
    ]


def happy_payloads(run_id: UUID, *, attempts: int = 1) -> list[AnyPayload]:
    """A completed run whose first `attempts - 1` attempts failed transiently."""
    op = operation_id_for(run_id)
    payloads: list[AnyPayload] = [created()]
    for n in range(1, attempts):
        payloads += [
            ModelCallStarted(operation_id=op, attempt=n),
            ModelCallFailed(operation_id=op, attempt=n, failure=failure()),
        ]
    return [
        *payloads,
        ModelCallStarted(operation_id=op, attempt=attempts),
        ModelResponseRecorded(
            operation_id=op, attempt=attempts, response=ModelResponse(content="hi")
        ),
        RunCompleted(),
    ]


_text = st.text(max_size=40)
_usage = st.none() | st.builds(
    Usage, input_tokens=st.integers(0, 10**6), output_tokens=st.integers(0, 10**6)
)
_times = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1), timezones=st.just(UTC)
)
_retryable = [k for k in FailureKind if k.retryable]
_failures = st.builds(ProviderFailure, kind=st.sampled_from(list(FailureKind)), message=_text)


@st.composite
def valid_payloads(draw: st.DrawFn, run_id: UUID) -> list[AnyPayload]:
    """A random legal walk through the run state machine, stopping at any point."""
    op = operation_id_for(run_id)
    attempts = draw(st.integers(0, 4))
    budget = RunBudget(max_output_tokens=16, max_attempts=draw(st.integers(max(attempts, 1), 6)))
    payloads: list[AnyPayload] = [
        created(
            model=draw(st.text(min_size=1, max_size=20)),
            input=draw(_text),
            instructions=draw(st.none() | _text),
            budget=budget,
            ambiguous_policy=draw(st.sampled_from(list(AmbiguousOutcomePolicy))),
        )
    ]
    if attempts == 0:
        if draw(st.booleans()):
            payloads.append(
                RunFailed(operation_id=op, reason=FailureReason.BUDGET_EXHAUSTED, message="late")
            )
        return payloads
    for n in range(1, attempts):  # earlier attempts failed in a retryable way
        retried = ProviderFailure(kind=draw(st.sampled_from(_retryable)), message=draw(_text))
        payloads += [
            ModelCallStarted(operation_id=op, attempt=n),
            ModelCallFailed(operation_id=op, attempt=n, failure=retried),
        ]
    payloads.append(ModelCallStarted(operation_id=op, attempt=attempts))
    ending = draw(st.sampled_from(["in_flight", "attempt_failed", "failed", "responded", "done"]))
    if ending in ("attempt_failed", "failed"):
        last = draw(_failures)
        payloads.append(ModelCallFailed(operation_id=op, attempt=attempts, failure=last))
        if ending == "failed":
            payloads.append(
                RunFailed(
                    operation_id=op, reason=_consistent_reason(last), message="x", kind=last.kind
                )
            )
    elif ending in ("responded", "done"):
        response = ModelResponse(
            content=draw(_text),
            usage=draw(_usage),
            stop_reason=draw(st.sampled_from(list(StopReason))),
            provider_request_id=draw(st.none() | st.text(min_size=1, max_size=20)),
        )
        payloads.append(ModelResponseRecorded(operation_id=op, attempt=attempts, response=response))
        if ending == "done":
            payloads.append(RunCompleted())
    return payloads


def _consistent_reason(last: ProviderFailure) -> FailureReason:
    if not last.kind.retryable:
        return FailureReason.PROVIDER_ERROR
    if last.outcome is CallOutcome.AMBIGUOUS:
        return FailureReason.AMBIGUOUS_OUTCOME
    return FailureReason.BUDGET_EXHAUSTED


@st.composite
def valid_logs(draw: st.DrawFn, *, min_size: int = 1) -> list[EventEnvelope]:
    run_id = draw(st.uuids())
    payloads = draw(valid_payloads(run_id).filter(lambda p: len(p) >= min_size))
    ids = draw(st.lists(st.uuids(), min_size=len(payloads), max_size=len(payloads), unique=True))
    return [
        new_event(
            event_id=ids[i], run_id=run_id, sequence=i + 1, occurred_at=draw(_times), payload=p
        )
        for i, p in enumerate(payloads)
    ]
