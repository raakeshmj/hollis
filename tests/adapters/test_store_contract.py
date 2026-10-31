"""The RunStore contract. Every adapter must pass this exact suite (see runtime/store.py)."""

import asyncio
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings

from hollis.runtime.events import EventEnvelope, ModelCallStarted
from hollis.runtime.runner import operation_id_for
from hollis.runtime.state import RunStatus
from hollis.runtime.store import (
    ConcurrencyConflict,
    DuplicateEventId,
    InvalidAppend,
    RunNotFound,
    RunStore,
    load_run,
)
from tests.support.events import build_log, created, happy_payloads, valid_logs
from tests.support.stores import STORE_KINDS, make_store


def _created(run_id: UUID) -> list[EventEnvelope]:
    return build_log(run_id, [created()])


async def test_create_then_read(store: RunStore) -> None:
    run = uuid4()
    log = _created(run)
    await store.append(run, 0, log)
    assert await store.read(run) == log


async def test_read_unknown_run_raises(store: RunStore) -> None:
    with pytest.raises(RunNotFound):
        await store.read(uuid4())


async def test_recreating_a_run_conflicts(store: RunStore) -> None:
    run = uuid4()
    await store.append(run, 0, _created(run))
    with pytest.raises(ConcurrencyConflict) as exc_info:
        await store.append(run, 0, _created(run))
    assert (exc_info.value.expected, exc_info.value.actual) == (0, 1)


async def test_events_are_read_in_sequence_order_across_appends(store: RunStore) -> None:
    run = uuid4()
    log = build_log(run, happy_payloads(run))
    for event in log:
        await store.append(run, event.sequence - 1, [event])
    assert [e.sequence for e in await store.read(run)] == [1, 2, 3, 4]
    assert await store.read(run) == log


async def test_multi_event_batch_appends_together(store: RunStore) -> None:
    run = uuid4()
    log = build_log(run, happy_payloads(run))
    await store.append(run, 0, log)
    assert (await load_run(store, run)).status is RunStatus.COMPLETED


@pytest.mark.parametrize("expected_version", [0, 2])
async def test_stale_or_future_expected_version_conflicts(
    store: RunStore, expected_version: int
) -> None:
    run = uuid4()
    log = build_log(run, happy_payloads(run))
    await store.append(run, 0, log[:1])
    # Sequences are made consistent with expected_version so only the version check fails.
    batch = [log[1].model_copy(update={"sequence": expected_version + 1})]
    with pytest.raises(ConcurrencyConflict) as exc_info:
        await store.append(run, expected_version, batch)
    assert exc_info.value.actual == 1
    assert await store.read(run) == log[:1]


async def test_append_to_unknown_run_with_nonzero_version_conflicts(store: RunStore) -> None:
    run = uuid4()
    event = build_log(run, [ModelCallStarted(operation_id="op", attempt=1)], start_sequence=4)
    with pytest.raises(ConcurrencyConflict):
        await store.append(run, 3, event)
    with pytest.raises(RunNotFound):
        await store.read(run)


async def test_duplicate_event_id_across_runs_is_rejected(store: RunStore) -> None:
    first, second = uuid4(), uuid4()
    log_a = _created(first)
    await store.append(first, 0, log_a)
    clash = [_created(second)[0].model_copy(update={"event_id": log_a[0].event_id})]
    with pytest.raises(DuplicateEventId):
        await store.append(second, 0, clash)
    with pytest.raises(RunNotFound):
        await store.read(second)


async def test_duplicate_event_id_within_batch_is_rejected(store: RunStore) -> None:
    run = uuid4()
    log = build_log(run, happy_payloads(run))[:2]
    log[1] = log[1].model_copy(update={"event_id": log[0].event_id})
    with pytest.raises(DuplicateEventId):
        await store.append(run, 0, log)
    with pytest.raises(RunNotFound):
        await store.read(run)


async def test_batch_failing_on_a_later_event_writes_nothing(store: RunStore) -> None:
    other, run = uuid4(), uuid4()
    existing = _created(other)
    await store.append(other, 0, existing)
    log = build_log(run, happy_payloads(run))
    log[2] = log[2].model_copy(update={"event_id": existing[0].event_id})
    with pytest.raises(DuplicateEventId):
        await store.append(run, 0, log)
    with pytest.raises(RunNotFound):
        await store.read(run)


async def test_retried_append_after_commit_conflicts_rather_than_duplicating(
    store: RunStore,
) -> None:
    run = uuid4()
    log = _created(run)
    await store.append(run, 0, log)
    with pytest.raises(ConcurrencyConflict):
        await store.append(run, 0, log)
    assert await store.read(run) == log


@pytest.mark.parametrize("case", ["empty", "wrong-run", "gap", "starts-wrong", "negative-version"])
async def test_malformed_batches_are_rejected(store: RunStore, case: str) -> None:
    run = uuid4()
    log = build_log(run, happy_payloads(run))
    expected_version = 0
    match case:
        case "empty":
            batch: list[EventEnvelope] = []
        case "wrong-run":
            batch = _created(uuid4())
        case "gap":
            batch = [log[0], log[2]]
        case "starts-wrong":
            batch = log[1:2]
        case _:
            batch, expected_version = log[:1], -1
    with pytest.raises(InvalidAppend):
        await store.append(run, expected_version, batch)
    with pytest.raises(RunNotFound):
        await store.read(run)


async def test_concurrent_appends_at_same_version_exactly_one_wins(store: RunStore) -> None:
    run = uuid4()
    await store.append(run, 0, _created(run))
    op = operation_id_for(run)
    attempts = [
        build_log(run, [ModelCallStarted(operation_id=op, attempt=1)], start_sequence=2)
        for _ in range(5)
    ]
    results = await asyncio.gather(
        *(store.append(run, 1, batch) for batch in attempts), return_exceptions=True
    )
    assert sum(r is None for r in results) == 1
    assert all(isinstance(r, ConcurrencyConflict) for r in results if r is not None)
    assert (await load_run(store, run)).version == 2


@pytest.mark.parametrize("kind", STORE_KINDS)
@settings(max_examples=25)
@given(log=valid_logs())
def test_generated_logs_round_trip(kind: str, log: list[EventEnvelope]) -> None:
    async def scenario() -> list[EventEnvelope]:
        with tempfile.TemporaryDirectory() as directory:
            store = await make_store(kind, Path(directory))
            split = len(log) // 2
            if split:
                await store.append(log[0].run_id, 0, log[:split])
            await store.append(log[0].run_id, split, log[split:])
            return await store.read(log[0].run_id)

    assert asyncio.run(scenario()) == log


async def test_load_run_folds_the_stored_log(store: RunStore) -> None:
    run = uuid4()
    await store.append(run, 0, build_log(run, happy_payloads(run))[:2])
    assert (await load_run(store, run)).status is RunStatus.MODEL_CALL_IN_FLIGHT
