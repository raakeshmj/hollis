"""Single-turn run execution.

`execute` derives the run's state from its event log and drives it to a terminal state, so the
same code path performs first execution, retries, and crash recovery. Every event is validated
with `apply` before it is appended, and every append is guarded by optimistic concurrency.

Model-call semantics (ADR-0006, amended by ADR-0007):
- Each physical attempt appends a `ModelCallStarted` intent first, and every attempt ends with a
  recorded outcome: `ModelResponseRecorded` or `ModelCallFailed`. Hollis owns all retries;
  providers must not retry internally.
- Once a response is recorded the provider is never invoked again for that call.
- An attempt whose outcome is unknown (timeout, server error, lost connection, or an intent found
  without an outcome on resume) is *ambiguous*: the provider may have generated and charged for
  a response. The run's `ambiguous_policy` decides whether to retry; the default retries only
  when the provider declares it deduplicates requests. Exactly-once execution is not claimed.

Secret handling (ADR-0009): failure messages and log records are redacted before they are
written; registered secret values are also removed from recorded response content. Run input is
never rewritten (recovery must resend the identical request), so `create` rejects input that
contains a registered secret.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from hollis.runtime.events import (
    AnyPayload,
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
from hollis.runtime.model import (
    CallOutcome,
    FailureKind,
    ModelCallError,
    ModelProvider,
    ModelRequest,
)
from hollis.runtime.policy import (
    MAX_RETRY_WAIT_S,
    AmbiguousOutcomePolicy,
    RunBudget,
    default_ambiguous_policy,
    retry_delay,
)
from hollis.runtime.redaction import Redactor
from hollis.runtime.state import RunState, RunStatus, apply
from hollis.runtime.store import RunStore, load_run

log = logging.getLogger("hollis.runtime")


class RunSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    input: str
    instructions: str | None = None
    budget: RunBudget
    ambiguous_policy: AmbiguousOutcomePolicy | None = None
    """None selects the default for the provider's capabilities."""


class SecretInRunInput(ValueError):
    """Run input or instructions contain a registered secret value; nothing was persisted."""


class ProviderMismatch(Exception):
    def __init__(self, run_id: UUID, recorded: str, actual: str) -> None:
        super().__init__(f"run {run_id} was created for provider {recorded!r}, not {actual!r}")


def operation_id_for(run_id: UUID, call_index: int = 0) -> str:
    """Stable idempotency key for a run's logical model call. M2 makes exactly one call."""
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
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        redactor: Redactor | None = None,
    ) -> None:
        self._store = store
        self._provider = provider
        self._clock = clock
        self._new_id = new_id
        self._sleep = sleep
        self._jitter = jitter
        self._redactor = redactor or Redactor()

    async def create(self, spec: RunSpec) -> RunState:
        if self._redactor.contains_secret(spec.input) or (
            spec.instructions is not None and self._redactor.contains_secret(spec.instructions)
        ):
            raise SecretInRunInput("run input or instructions contain a registered secret")
        capabilities = self._provider.capabilities
        policy = spec.ambiguous_policy or default_ambiguous_policy(
            provider_deduplicates=capabilities.deduplicates_requests
        )
        payload = RunCreated(
            model=spec.model,
            input=spec.input,
            instructions=spec.instructions,
            provider=capabilities.name,
            budget=spec.budget,
            ambiguous_policy=policy,
        )
        return await self._append(None, self._new_id(), payload)

    async def execute(self, run_id: UUID) -> RunState:
        """Drive a run to a terminal state. Idempotent: a terminal run is returned unchanged."""
        state = await load_run(self._store, run_id)
        if state.status.is_terminal:
            return state
        provider = self._provider.capabilities.name
        if state.provider != provider:
            raise ProviderMismatch(run_id, state.provider, provider)
        while not state.status.is_terminal:
            state = await self._advance(state)
        return state

    async def _advance(self, state: RunState) -> RunState:
        match state.status:
            case RunStatus.MODEL_RESPONDED:
                return await self._append(state, state.run_id, RunCompleted())
            case RunStatus.MODEL_CALL_IN_FLIGHT:
                # Found on resume: the intent is durable but the outcome never was.
                return await self._record_failure(
                    state,
                    ProviderFailure(
                        kind=FailureKind.INTERRUPTED,
                        message=f"attempt {state.attempts} has no recorded outcome",
                    ),
                )
            case RunStatus.ATTEMPT_FAILED:
                return await self._after_failure(state)
            case _:  # PENDING
                return await self._attempt(state)

    async def _after_failure(self, state: RunState) -> RunState:
        failure = state.last_failure
        if failure is None:  # unreachable: ATTEMPT_FAILED always records a failure
            raise AssertionError("attempt_failed without last_failure")
        if not failure.kind.retryable:
            return await self._fail(state, FailureReason.PROVIDER_ERROR, failure.message, failure)
        if (
            failure.outcome is CallOutcome.AMBIGUOUS
            and state.ambiguous_policy is AmbiguousOutcomePolicy.FAIL
        ):
            message = (
                f"outcome of attempt {state.attempts} is unknown ({failure.kind}); not retried"
            )
            return await self._fail(state, FailureReason.AMBIGUOUS_OUTCOME, message, failure)
        if state.attempts >= state.budget.max_attempts:
            message = f"max_attempts {state.budget.max_attempts} reached"
            return await self._fail(state, FailureReason.BUDGET_EXHAUSTED, message, failure)

        delay = retry_delay(state.attempts, failure.retry_after_s, self._jitter)
        remaining = self._remaining(state)
        if delay > MAX_RETRY_WAIT_S or (remaining is not None and delay >= remaining):
            message = f"retry would wait {delay:.1f}s, beyond the deadline or wait limit"
            return await self._fail(state, FailureReason.BUDGET_EXHAUSTED, message, failure)
        log.info("run %s: retrying after %.2fs (%s)", state.run_id, delay, failure.kind)
        await self._sleep(delay)
        return await self._attempt(state)

    async def _attempt(self, state: RunState) -> RunState:
        remaining = self._remaining(state)
        if remaining is not None and remaining <= 0:
            return await self._fail(state, FailureReason.BUDGET_EXHAUSTED, "deadline reached", None)
        timeout_s = state.budget.attempt_timeout_s
        if remaining is not None:
            timeout_s = min(timeout_s, remaining)

        operation_id = operation_id_for(state.run_id)
        intent = ModelCallStarted(operation_id=operation_id, attempt=state.attempts + 1)
        state = await self._append(state, state.run_id, intent)
        request = ModelRequest(
            operation_id=operation_id,
            model=state.model,
            input=state.input,
            instructions=state.instructions,
            max_output_tokens=state.budget.max_output_tokens,
            timeout_s=timeout_s,
        )
        try:
            async with asyncio.timeout(timeout_s):
                response = await self._provider.generate(request)
        except ModelCallError as exc:
            return await self._record_failure(state, ProviderFailure.from_error(exc))
        except TimeoutError:
            failure = ProviderFailure(
                kind=FailureKind.TIMEOUT, message=f"no response within {timeout_s:.1f}s"
            )
            return await self._record_failure(state, failure)
        # Any other exception propagates: the run stays in flight and resume records it as
        # an interrupted (ambiguous) attempt.
        content = self._redactor.redact_known(response.content)
        if content != response.content:
            response = response.model_copy(update={"content": content})
        recorded = ModelResponseRecorded(
            operation_id=operation_id, attempt=state.attempts, response=response
        )
        return await self._append(state, state.run_id, recorded)

    async def _record_failure(self, state: RunState, failure: ProviderFailure) -> RunState:
        failure = failure.model_copy(update={"message": self._redactor.redact(failure.message)})
        log.warning(
            "run %s attempt %d failed: %s (%s): %s",
            state.run_id,
            state.attempts,
            failure.kind,
            failure.outcome,
            failure.message,
        )
        payload = ModelCallFailed(
            operation_id=operation_id_for(state.run_id), attempt=state.attempts, failure=failure
        )
        return await self._append(state, state.run_id, payload)

    async def _fail(
        self,
        state: RunState,
        reason: FailureReason,
        message: str,
        failure: ProviderFailure | None,
    ) -> RunState:
        message = self._redactor.redact(message)
        log.warning("run %s failed: %s: %s", state.run_id, reason, message)
        payload = RunFailed(
            operation_id=operation_id_for(state.run_id),
            reason=reason,
            message=message,
            kind=failure.kind if failure else None,
        )
        return await self._append(state, state.run_id, payload)

    def _remaining(self, state: RunState) -> float | None:
        deadline = state.deadline
        if deadline is None:
            return None
        return (deadline - self._clock()).total_seconds()

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
