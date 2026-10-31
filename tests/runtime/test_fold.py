from uuid import uuid4

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from hollis.runtime.events import (
    AnyPayload,
    EventEnvelope,
    ModelCallStarted,
    ModelResponseRecorded,
    RunCompleted,
    RunCreated,
    RunFailed,
)
from hollis.runtime.model import ModelResponse
from hollis.runtime.state import (
    IllegalTransition,
    InvalidEventSequence,
    RunState,
    RunStatus,
    apply,
    fold,
)
from tests.support.events import build_log, happy_payloads, operation_id_for, valid_logs

RUN = uuid4()
OP = operation_id_for(RUN)


def test_happy_path_folds_to_completed() -> None:
    state = fold(build_log(RUN, happy_payloads(RUN)))
    assert state.status is RunStatus.COMPLETED
    assert state.version == 4
    assert state.operation_id == OP
    assert state.attempts == 1
    assert state.response == ModelResponse(content="hi")
    assert state.failure is None


def test_retries_are_counted_as_attempts() -> None:
    state = fold(build_log(RUN, happy_payloads(RUN, attempts=3)))
    assert state.attempts == 3
    assert state.status is RunStatus.COMPLETED


def test_failure_is_recorded() -> None:
    log = build_log(
        RUN,
        [
            RunCreated(model="m", input="x"),
            ModelCallStarted(operation_id=OP, attempt=1),
            RunFailed(operation_id=OP, error_type="Rejected", message="nope"),
        ],
    )
    state = fold(log)
    assert state.status is RunStatus.FAILED
    assert state.failure is not None
    assert state.failure.error_type == "Rejected"


@pytest.mark.parametrize(
    ("payloads", "reason"),
    [
        ([], "empty"),
        ([ModelCallStarted(operation_id=OP, attempt=1)], "first event not RunCreated"),
        ([RunCreated(model="m", input="x"), RunCreated(model="m", input="x")], "created twice"),
        (
            [RunCreated(model="m", input="x"), ModelCallStarted(operation_id=OP, attempt=2)],
            "attempt skips",
        ),
        (
            [
                RunCreated(model="m", input="x"),
                ModelCallStarted(operation_id=OP, attempt=1),
                ModelCallStarted(operation_id="other", attempt=2),
            ],
            "operation id changes between attempts",
        ),
        (
            [
                RunCreated(model="m", input="x"),
                ModelCallStarted(operation_id=OP, attempt=1),
                ModelResponseRecorded(
                    operation_id="other", attempt=1, response=ModelResponse(content="x")
                ),
            ],
            "response for another operation",
        ),
        (
            [
                RunCreated(model="m", input="x"),
                ModelCallStarted(operation_id=OP, attempt=1),
                ModelCallStarted(operation_id=OP, attempt=2),
                ModelResponseRecorded(
                    operation_id=OP, attempt=1, response=ModelResponse(content="x")
                ),
            ],
            "response attributed to a stale attempt",
        ),
        (
            [
                RunCreated(model="m", input="x"),
                ModelCallStarted(operation_id=OP, attempt=1),
                RunFailed(operation_id="other", error_type="E", message="m"),
            ],
            "failure for another operation",
        ),
        ([RunCreated(model="m", input="x"), RunCompleted()], "completed without response"),
        ([*happy_payloads(RUN), RunCompleted()], "event after terminal"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_invalid_sequences_fail_loudly(payloads: list[AnyPayload], reason: str) -> None:
    with pytest.raises(InvalidEventSequence):
        fold(build_log(RUN, payloads))


def test_sequence_must_start_at_one() -> None:
    with pytest.raises(InvalidEventSequence, match="expected sequence 1"):
        fold(build_log(RUN, [RunCreated(model="m", input="x")], start_sequence=2))


def test_mixed_run_ids_are_rejected() -> None:
    log = build_log(RUN, happy_payloads(RUN))
    log[1] = log[1].model_copy(update={"run_id": uuid4()})
    with pytest.raises(InvalidEventSequence, match="in log of run"):
        fold(log)


def test_duplicate_event_ids_are_rejected() -> None:
    log = build_log(RUN, happy_payloads(RUN))
    log[2] = log[2].model_copy(update={"event_id": log[0].event_id})
    with pytest.raises(InvalidEventSequence, match="duplicate event_id"):
        fold(log)


def test_illegal_transition_is_an_invalid_sequence() -> None:
    assert issubclass(IllegalTransition, InvalidEventSequence)


# --- properties over generated logs -------------------------------------------------------


def _apply_all(log: list[EventEnvelope]) -> RunState:
    state: RunState | None = None
    for event in log:
        state = apply(state, event)
    assert state is not None
    return state


@given(valid_logs())
def test_valid_logs_fold_and_version_matches_length(log: list[EventEnvelope]) -> None:
    state = fold(log)
    assert state.version == len(log)
    assert state.run_id == log[0].run_id
    assert state == _apply_all(log)


@given(valid_logs())
def test_every_prefix_of_a_valid_log_is_valid(log: list[EventEnvelope]) -> None:
    for end in range(1, len(log) + 1):
        assert fold(log[:end]).version == end


def _mutations(log: list[EventEnvelope], data: st.DataObject) -> list[EventEnvelope]:
    kind = data.draw(st.sampled_from(["drop", "swap", "duplicate", "run_id", "sequence"]))
    i = data.draw(st.integers(0, len(log) - 2))
    mutated = list(log)
    match kind:
        case "drop":  # dropping any event but the last leaves a gap or a missing RunCreated
            del mutated[i]
        case "swap":
            mutated[i], mutated[i + 1] = mutated[i + 1], mutated[i]
        case "duplicate":
            mutated.insert(i + 1, mutated[i])
        case "run_id":
            mutated[i + 1] = mutated[i + 1].model_copy(update={"run_id": uuid4()})
        case _:
            new_seq = data.draw(st.integers(1, 100).filter(lambda s: s != mutated[i].sequence))
            mutated[i] = mutated[i].model_copy(update={"sequence": new_seq})
    return mutated


@given(valid_logs(min_size=2), st.data())
def test_structural_mutations_are_rejected(log: list[EventEnvelope], data: st.DataObject) -> None:
    with pytest.raises(InvalidEventSequence):
        fold(_mutations(log, data))


@given(valid_logs(min_size=3), st.data())
def test_operation_and_attempt_mutations_are_rejected(
    log: list[EventEnvelope], data: st.DataObject
) -> None:
    # Indices of events after the first ModelCallStarted that carry an operation id.
    later = [
        i
        for i, e in enumerate(log)
        if i >= 2 and isinstance(e.payload, ModelCallStarted | ModelResponseRecorded | RunFailed)
    ]
    assume(later)
    i = data.draw(st.sampled_from(later))
    payload = log[i].payload
    assert isinstance(payload, ModelCallStarted | ModelResponseRecorded | RunFailed)
    if data.draw(st.booleans()) or isinstance(payload, RunFailed):
        changed = payload.model_copy(update={"operation_id": "tampered"})
    else:
        bump = data.draw(st.sampled_from([-1, 1]))
        changed = payload.model_copy(update={"attempt": payload.attempt + bump})
    mutated = list(log)
    mutated[i] = log[i].model_copy(update={"payload": changed})
    with pytest.raises(InvalidEventSequence):
        fold(mutated)
