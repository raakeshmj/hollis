from uuid import uuid4

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from hollis.runtime.events import (
    AnyPayload,
    EventEnvelope,
    FailureReason,
    ModelCallFailed,
    ModelCallStarted,
    ModelResponseRecorded,
    RunCompleted,
    RunFailed,
)
from hollis.runtime.model import FailureKind, ModelResponse, Usage
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import operation_id_for
from hollis.runtime.state import (
    IllegalTransition,
    InvalidEventSequence,
    RunState,
    RunStatus,
    apply,
    fold,
)
from tests.support.events import build_log, created, failure, happy_payloads, valid_logs

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
    assert state.last_failure is None


def test_retries_are_counted_as_attempts() -> None:
    state = fold(build_log(RUN, happy_payloads(RUN, attempts=3)))
    assert state.attempts == 3
    assert state.status is RunStatus.COMPLETED
    assert state.last_failure == failure()


def test_run_state_carries_configuration_from_run_created() -> None:
    state = fold(build_log(RUN, [created(budget=RunBudget(max_output_tokens=9, deadline_s=60))]))
    assert state.provider == "scripted-fake"
    assert state.budget.max_output_tokens == 9
    assert state.deadline is not None
    assert (state.deadline - state.created_at).total_seconds() == 60


def test_usage_is_accumulated_from_recorded_responses() -> None:
    reply = ModelResponse(content="x", usage=Usage(input_tokens=7, output_tokens=3))
    log = build_log(
        RUN,
        [
            created(),
            ModelCallStarted(operation_id=OP, attempt=1),
            ModelResponseRecorded(operation_id=OP, attempt=1, response=reply),
        ],
    )
    assert fold(log).usage.model_dump() == {"input_tokens": 7, "output_tokens": 3}


def test_failure_is_recorded() -> None:
    rejected = failure(FailureKind.INVALID_REQUEST, "bad")
    log = build_log(
        RUN,
        [
            created(),
            ModelCallStarted(operation_id=OP, attempt=1),
            ModelCallFailed(operation_id=OP, attempt=1, failure=rejected),
            RunFailed(
                operation_id=OP,
                reason=FailureReason.PROVIDER_ERROR,
                message="bad",
                kind=FailureKind.INVALID_REQUEST,
            ),
        ],
    )
    state = fold(log)
    assert state.status is RunStatus.FAILED
    assert state.failure is not None
    assert (state.failure.reason, state.failure.kind) == (
        FailureReason.PROVIDER_ERROR,
        FailureKind.INVALID_REQUEST,
    )
    assert state.last_failure == rejected


def _started(n: int) -> ModelCallStarted:
    return ModelCallStarted(operation_id=OP, attempt=n)


def _failed(n: int, kind: FailureKind = FailureKind.OVERLOADED) -> ModelCallFailed:
    return ModelCallFailed(operation_id=OP, attempt=n, failure=failure(kind))


def _run_failed(reason: FailureReason, op: str = OP) -> RunFailed:
    return RunFailed(operation_id=op, reason=reason, message="m")


_REPLY = ModelResponse(content="x")


@pytest.mark.parametrize(
    ("payloads", "reason"),
    [
        ([], "empty"),
        ([_started(1)], "first event not RunCreated"),
        ([created(), created()], "created twice"),
        ([created(), _started(2)], "attempt skips"),
        ([created(), _started(1), _started(2)], "retry without a recorded outcome"),
        (
            [created(), _started(1), _failed(1), ModelCallStarted(operation_id="other", attempt=2)],
            "operation id changes between attempts",
        ),
        (
            [
                created(),
                _started(1),
                ModelResponseRecorded(operation_id="other", attempt=1, response=_REPLY),
            ],
            "response for another operation",
        ),
        (
            [
                created(),
                _started(1),
                _failed(1),
                _started(2),
                ModelResponseRecorded(operation_id=OP, attempt=1, response=_REPLY),
            ],
            "response attributed to a stale attempt",
        ),
        ([created(), _started(1), _failed(2)], "failure for another attempt"),
        (
            [created(), _started(1), _failed(1), _run_failed(FailureReason.BUDGET_EXHAUSTED, "x")],
            "run failure for another operation",
        ),
        ([created(), _started(1), _run_failed(FailureReason.BUDGET_EXHAUSTED)], "fail in flight"),
        (
            [created(), _started(1), _failed(1), _run_failed(FailureReason.PROVIDER_ERROR)],
            "provider_error after a retryable failure",
        ),
        (
            [created(), _started(1), _failed(1), _run_failed(FailureReason.AMBIGUOUS_OUTCOME)],
            "ambiguous_outcome after a not-processed failure",
        ),
        (
            [
                created(budget=RunBudget(max_output_tokens=1, max_attempts=1)),
                _started(1),
                _failed(1),
                _started(2),
            ],
            "attempt beyond max_attempts",
        ),
        ([created(), _started(1), RunCompleted()], "completed without response"),
        ([*happy_payloads(RUN), RunCompleted()], "event after terminal"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_invalid_sequences_fail_loudly(payloads: list[AnyPayload], reason: str) -> None:
    with pytest.raises(InvalidEventSequence):
        fold(build_log(RUN, payloads))


def test_sequence_must_start_at_one() -> None:
    with pytest.raises(InvalidEventSequence, match="expected sequence 1"):
        fold(build_log(RUN, [created()], start_sequence=2))


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


_OP_CARRYING = ModelCallStarted | ModelResponseRecorded | ModelCallFailed | RunFailed


@given(valid_logs(min_size=3), st.data())
def test_operation_and_attempt_mutations_are_rejected(
    log: list[EventEnvelope], data: st.DataObject
) -> None:
    # Indices of events after the first ModelCallStarted that carry an operation id.
    later = [i for i, e in enumerate(log) if i >= 2 and isinstance(e.payload, _OP_CARRYING)]
    assume(later)
    i = data.draw(st.sampled_from(later))
    payload = log[i].payload
    assert isinstance(payload, _OP_CARRYING)
    if data.draw(st.booleans()) or isinstance(payload, RunFailed):
        changed = payload.model_copy(update={"operation_id": "tampered"})
    else:
        bump = data.draw(st.sampled_from([-1, 1]))
        changed = payload.model_copy(update={"attempt": payload.attempt + bump})
    mutated = list(log)
    mutated[i] = log[i].model_copy(update={"payload": changed})
    with pytest.raises(InvalidEventSequence):
        fold(mutated)
