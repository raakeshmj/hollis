import itertools

import pytest

from hollis.runtime.events import EventType
from hollis.runtime.state import TRANSITIONS, IllegalTransition, RunStatus, next_status

S = RunStatus
E = EventType

# Written out independently of TRANSITIONS so the table itself is under test.
LEGAL: dict[tuple[RunStatus | None, EventType], RunStatus] = {
    (None, E.RUN_CREATED): S.PENDING,
    (S.PENDING, E.MODEL_CALL_STARTED): S.MODEL_CALL_IN_FLIGHT,
    (S.PENDING, E.RUN_FAILED): S.FAILED,
    (S.MODEL_CALL_IN_FLIGHT, E.MODEL_RESPONSE_RECORDED): S.MODEL_RESPONDED,
    (S.MODEL_CALL_IN_FLIGHT, E.MODEL_CALL_FAILED): S.ATTEMPT_FAILED,
    (S.ATTEMPT_FAILED, E.MODEL_CALL_STARTED): S.MODEL_CALL_IN_FLIGHT,
    (S.ATTEMPT_FAILED, E.RUN_FAILED): S.FAILED,
    (S.MODEL_RESPONDED, E.RUN_COMPLETED): S.COMPLETED,
}

ALL_PAIRS = list(itertools.product([None, *RunStatus], EventType))
ILLEGAL = [pair for pair in ALL_PAIRS if pair not in LEGAL]


def test_table_matches_specification() -> None:
    assert dict(TRANSITIONS) == LEGAL


@pytest.mark.parametrize(("pair", "expected"), LEGAL.items(), ids=str)
def test_legal_transition(pair: tuple[RunStatus | None, EventType], expected: RunStatus) -> None:
    assert next_status(*pair) == expected


@pytest.mark.parametrize("pair", ILLEGAL, ids=str)
def test_illegal_transition_raises(pair: tuple[RunStatus | None, EventType]) -> None:
    with pytest.raises(IllegalTransition):
        next_status(*pair)


@pytest.mark.parametrize("status", [S.COMPLETED, S.FAILED])
def test_terminal_states_accept_no_events(status: RunStatus) -> None:
    assert status.is_terminal
    assert not any(current == status for current, _ in TRANSITIONS)


def test_every_pair_is_classified() -> None:
    assert len(LEGAL) + len(ILLEGAL) == len(ALL_PAIRS) == 7 * 6
