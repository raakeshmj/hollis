"""Single-turn run execution (M1).

`execute` derives the run's state from its event log and drives it to a terminal state, so the
same code path performs first execution and crash recovery. Every event is validated with
`apply` before it is appended, and every append is guarded by optimistic concurrency.

Model-call semantics (ADR-0006): a `ModelCallStarted` intent is persisted before each physical
attempt, and every attempt of the logical call uses the same operation id. Once a
`ModelResponseRecorded` is durable the provider is never invoked again for that call. If a
crash lands between generation and recording, the outcome is ambiguous and the call is retried
under the same operation id: invocation is at-least-once, recording is at-most-once. This is
not exactly-once execution unless the provider deduplicates by operation id.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from hollis.runtime.events import (
    AnyPayload,
    ModelCallStarted,
    ModelResponseRecorded,
    RunCompleted,
    RunCreated,
    RunFailed,
    new_event,
)
from hollis.runtime.model import ModelProvider, ModelProviderError, ModelRequest
from hollis.runtime.state import RunState, RunStatus, apply
from hollis.runtime.store import RunStore, load_run


class RunSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    input: str
    instructions: str | None = None


def operation_id_for(run_id: UUID, call_index: int = 0) -> str:
    """Stable idempotency key for a run's logical model call. M1 makes exactly one call."""
    return f"{run_id}/model/{call_index}"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Runner:
    def __init__(
        self,
        store: RunStore,
        provider: ModelProvider,
        *,
        clock: Callable[[], datetime] = _utc_now,
        new_id: Callable[[], UUID] = uuid4,
    ) -> None:
        self._store = store
        self._provider = provider
        self._clock = clock
        self._new_id = new_id

    async def create(self, spec: RunSpec) -> RunState:
        payload = RunCreated(model=spec.model, input=spec.input, instructions=spec.instructions)
        return await self._append(None, self._new_id(), payload)

    async def execute(self, run_id: UUID) -> RunState:
        """Drive a run to a terminal state. Idempotent: a terminal run is returned unchanged."""
        state = await load_run(self._store, run_id)
        while not state.status.is_terminal:
            state = await self._advance(state)
        return state

    async def _advance(self, state: RunState) -> RunState:
        if state.status is RunStatus.MODEL_RESPONDED:
            return await self._append(state, state.run_id, RunCompleted())

        # PENDING or MODEL_CALL_IN_FLIGHT: persist the attempt intent, then invoke.
        operation_id = operation_id_for(state.run_id)
        intent = ModelCallStarted(operation_id=operation_id, attempt=state.attempts + 1)
        state = await self._append(state, state.run_id, intent)
        request = ModelRequest(
            operation_id=operation_id,
            model=state.model,
            input=state.input,
            instructions=state.instructions,
        )
        try:
            response = await self._provider.generate(request)
        except ModelProviderError as exc:
            failure = RunFailed(
                operation_id=operation_id, error_type=type(exc).__name__, message=str(exc)
            )
            return await self._append(state, state.run_id, failure)
        # Any other exception propagates: the outcome is ambiguous and the run stays in flight.
        recorded = ModelResponseRecorded(
            operation_id=operation_id, attempt=state.attempts, response=response
        )
        return await self._append(state, state.run_id, recorded)

    async def _append(self, state: RunState | None, run_id: UUID, payload: AnyPayload) -> RunState:
        version = state.version if state else 0
        event = new_event(
            event_id=self._new_id(),
            run_id=run_id,
            sequence=version + 1,
            occurred_at=self._clock(),
            payload=payload,
        )
        next_state = apply(state, event)  # never persist an event the fold would reject
        await self._store.append(run_id, version, [event])
        return next_state
