"""Deterministic builders and Hypothesis strategies for run event logs."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from hypothesis import strategies as st

from hollis.runtime.events import (
    AnyPayload,
    EventEnvelope,
    ModelCallStarted,
    ModelResponseRecorded,
    RunCompleted,
    RunCreated,
    RunFailed,
    new_event,
)
from hollis.runtime.model import ModelResponse, Usage

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def operation_id_for(run_id: UUID) -> str:
    return f"{run_id}/model/0"


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
    op = operation_id_for(run_id)
    return [
        RunCreated(model="fake", input="hello"),
        *(ModelCallStarted(operation_id=op, attempt=n) for n in range(1, attempts + 1)),
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


@st.composite
def valid_payloads(draw: st.DrawFn, run_id: UUID) -> list[AnyPayload]:
    """A random legal walk through the run state machine, stopping at any point."""
    op = operation_id_for(run_id)
    payloads: list[AnyPayload] = [
        RunCreated(
            model=draw(st.text(min_size=1, max_size=20)),
            input=draw(_text),
            instructions=draw(st.none() | _text),
        )
    ]
    attempts = draw(st.integers(0, 4))
    payloads += [ModelCallStarted(operation_id=op, attempt=n) for n in range(1, attempts + 1)]
    if attempts == 0:
        return payloads
    ending = draw(st.sampled_from(["in_flight", "responded", "completed", "failed"]))
    if ending == "failed":
        payloads.append(RunFailed(operation_id=op, error_type=draw(_text), message=draw(_text)))
    elif ending in ("responded", "completed"):
        response = ModelResponse(content=draw(_text), usage=draw(_usage))
        payloads.append(ModelResponseRecorded(operation_id=op, attempt=attempts, response=response))
        if ending == "completed":
            payloads.append(RunCompleted())
    return payloads


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
