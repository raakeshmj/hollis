"""Bounded multi-turn run execution with tools.

`execute` derives the run's state from its event log and drives it forward, so the same code path
performs first execution, retries, approvals and crash recovery. Every event is validated with
`apply` before it is appended, and every append is guarded by optimistic concurrency.

A run alternates model turns and tool phases until the model answers without tool calls, a limit
is reached, or the run waits for human approval:

    model turn -> tool calls decided (validation + policy) -> approvals -> execution -> results
               -> next model turn ...

Model-call semantics (ADR-0006, amended by ADR-0007) are unchanged per turn: an intent before
every attempt, a recorded outcome after it, Hollis-owned retries, and an explicit policy for
ambiguous outcomes. Tool semantics (ADR-0014):
- A tool call is a request. It runs only after validation, a deterministic policy decision, and
  (where required) an approval bound to the exact invocation, all recorded before execution.
- `ToolExecutionStarted` is recorded before a tool runs. After a crash, an invocation that
  started but has no result is re-executed only if its tool is idempotent, reconciled if the tool
  supports it, and otherwise recorded as ambiguous. It is never blindly repeated.
- Results are redacted, size-limited and (for external tools) framed as untrusted data.

Context (ADR-0018): before each new logical model call the runner recalls memories (once, at run
start), compacts older turns when the plan requires it, and records the assembled context. Every
attempt of the call sends the request built from that record. Large tool results are stored whole
in the artifact store, when one is configured (ADR-0019).

Secret handling (ADR-0009): failure messages, tool results and log records are redacted;
registered secrets are removed from recorded model output, including tool-call arguments, and
calls whose arguments contained one are denied. Run input is never rewritten; `create` rejects
input that contains a registered secret.
"""

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from hollis.runtime import assembly
from hollis.runtime.artifacts import ArtifactRef, ArtifactStore
from hollis.runtime.compaction import (
    DeterministicSummarizer,
    Summarizer,
    SummarizeRequest,
    deterministic_draft,
)
from hollis.runtime.context import (
    CompactionMethod,
    ContextPolicy,
    SummaryRejected,
    merge_summary,
)
from hollis.runtime.conversation import ToolSpec
from hollis.runtime.events import (
    AnyPayload,
    ContextCompacted,
    FailureReason,
    MemoryRecalled,
    ModelCallFailed,
    ModelCallStarted,
    ModelResponseRecorded,
    ProviderFailure,
    RunCompleted,
    RunCreated,
    RunFailed,
    ToolApprovalResolved,
    ToolCallDecided,
    ToolExecutionStarted,
    ToolOutcome,
    ToolResultRecorded,
    new_event,
)
from hollis.runtime.memory import MemoryAccess, MemoryRecall, RecallQuery
from hollis.runtime.model import (
    CallOutcome,
    DeploymentRef,
    FailureKind,
    ModelCallError,
    ModelProvider,
    ModelResolver,
    ModelResponse,
    SingleProvider,
    TokenCounter,
    require_text_generation,
)
from hollis.runtime.policy import (
    MAX_RETRY_WAIT_S,
    AmbiguousOutcomePolicy,
    RunBudget,
    default_ambiguous_policy,
    retry_delay,
)
from hollis.runtime.redaction import REDACTED, Redactor
from hollis.runtime.results import (
    artifact_preview,
    finalize,
    redact_json,
    redact_parts,
    render_parts,
)
from hollis.runtime.state import (
    Invocation,
    RunState,
    RunStatus,
    apply,
    operation_id_for,
)
from hollis.runtime.store import RunStore, load_run
from hollis.runtime.tools import (
    ApprovalDecision,
    ApprovalRequest,
    Approver,
    Decision,
    DefaultToolPolicy,
    PolicyRequest,
    Tool,
    ToolArgumentError,
    ToolCatalog,
    ToolContext,
    ToolEffect,
    ToolError,
    ToolOutput,
    ToolPolicy,
    ToolTrust,
    approval_binding,
)

log = logging.getLogger("hollis.runtime")

__all__ = [
    "ApprovalMismatch",
    "DeploymentChanged",
    "RunSpec",
    "Runner",
    "SecretInRunInput",
    "ToolChanged",
    "UnknownTool",
    "operation_id_for",
]


class RunSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    """Logical model (deployment) id, resolved through the model resolver."""
    input: str
    instructions: str | None = None
    budget: RunBudget
    ambiguous_policy: AmbiguousOutcomePolicy | None = None
    """None selects the default for the deployment's idempotency behaviour."""
    tools: tuple[str, ...] = ()
    """Names of catalog tools this run may use. Nothing else is offered to or callable by the
    model (least privilege per run)."""
    context: ContextPolicy = ContextPolicy()
    memory: MemoryAccess | None = None
    """Memory namespaces this run may read and write. None: no memory."""


class SecretInRunInput(ValueError):
    """Run input or instructions contain a registered secret value; nothing was persisted."""


class UnknownTool(LookupError):
    """A run named a tool the catalog does not have; nothing was persisted."""


class DeploymentChanged(Exception):
    """The run's deployment now resolves to a different identity; resuming is refused."""

    def __init__(self, run_id: UUID, recorded: DeploymentRef, actual: DeploymentRef) -> None:
        self.recorded = recorded
        self.actual = actual
        changed = sorted(
            name for name, value in recorded.model_dump().items() if getattr(actual, name) != value
        )
        super().__init__(f"run {run_id}: deployment {recorded.deployment!r} changed ({changed})")


class ToolChanged(Exception):
    """A tool enabled for this run is missing or its contract changed; resuming is refused."""


class ApprovalMismatch(ValueError):
    """An approval decision does not match any pending invocation of the run."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _Writer:
    """Serializes appends so concurrent tool executions update one consistent state."""

    def __init__(self, runner: "Runner", state: RunState) -> None:
        self._runner = runner
        self.state = state
        self._lock = asyncio.Lock()

    async def append(self, payload: AnyPayload) -> RunState:
        async with self._lock:
            self.state = await self._runner._append(self.state, self.state.run_id, payload)  # pyright: ignore[reportPrivateUsage]
            return self.state


class Runner:
    def __init__(
        self,
        store: RunStore,
        models: ModelResolver | ModelProvider,
        *,
        tools: ToolCatalog | None = None,
        policy: ToolPolicy | None = None,
        approver: Approver | None = None,
        environment: str | None = None,
        clock: Callable[[], datetime] = _utc_now,
        new_id: Callable[[], UUID] = uuid4,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        redactor: Redactor | None = None,
        artifacts: ArtifactStore | None = None,
        memory: MemoryRecall | None = None,
        summarizer: Summarizer | None = None,
    ) -> None:
        """`policy` defaults to DefaultToolPolicy (trusted read-only tools allowed, everything
        else requires approval). Without an `approver`, calls requiring approval are denied.
        Without `artifacts`, large tool results are truncated (M3 behaviour). Without `memory`,
        nothing is recalled. `summarizer` defaults to the deterministic one."""
        self._store = store
        self._models: ModelResolver = (
            models if isinstance(models, ModelResolver) else SingleProvider(models)
        )
        self._tools = tools
        self._policy: ToolPolicy = policy or DefaultToolPolicy()
        self._approver = approver
        self._environment = environment
        self._clock = clock
        self._new_id = new_id
        self._sleep = sleep
        self._jitter = jitter
        self._redactor = redactor or Redactor()
        self._artifacts = artifacts
        self._memory = memory
        self._summarizer: Summarizer = summarizer or DeterministicSummarizer()

    # --- public API -----------------------------------------------------------------------------

    async def create(self, spec: RunSpec) -> RunState:
        """Validate and persist a new run. Nothing is persisted if validation fails."""
        # Resolving first applies routing policy and registers the deployment's secrets.
        descriptor = self._models.resolve(spec.model).descriptor
        if self._redactor.contains_secret(spec.input) or (
            spec.instructions is not None and self._redactor.contains_secret(spec.instructions)
        ):
            raise SecretInRunInput("run input or instructions contain a registered secret")
        require_text_generation(
            descriptor,
            has_instructions=spec.instructions is not None,
            max_output_tokens=spec.budget.max_output_tokens,
            uses_tools=bool(spec.tools),
        )
        refs = tuple(self._require_tool(name).descriptor.ref for name in spec.tools)
        if len({r.name for r in refs}) != len(refs):
            raise ValueError("duplicate tool names in RunSpec.tools")
        policy = spec.ambiguous_policy or default_ambiguous_policy(
            provider_deduplicates=descriptor.deduplicates_requests
        )
        payload = RunCreated(
            model=spec.model,
            input=spec.input,
            instructions=spec.instructions,
            deployment=descriptor.ref,
            budget=spec.budget,
            ambiguous_policy=policy,
            tools=refs,
            context=spec.context,
            memory=spec.memory,
        )
        return await self._append(None, self._new_id(), payload)

    async def execute(self, run_id: UUID) -> RunState:
        """Drive a run until it is terminal or waiting for approval. Idempotent: a terminal run
        is returned unchanged; a run waiting for approval is returned with
        `state.awaiting_approval` non-empty."""
        state = await load_run(self._store, run_id)
        if state.status.is_terminal:
            return state
        provider = self._models.resolve(state.model)
        if provider.descriptor.ref != state.deployment:
            raise DeploymentChanged(run_id, state.deployment, provider.descriptor.ref)
        tools = self._verify_tools(state)
        while not state.status.is_terminal:
            advanced = await self._advance(state, provider, tools)
            if advanced.version == state.version:
                return advanced  # blocked on a pending approval
            state = advanced
        return state

    async def pending_approvals(self, run_id: UUID) -> list[ApprovalRequest]:
        state = await load_run(self._store, run_id)
        return [self._approval_request(state, i) for i in state.awaiting_approval]

    async def resolve_approval(self, run_id: UUID, decision: ApprovalDecision) -> RunState:
        """Record a human decision for a pending invocation. The decision must carry the exact
        binding of the pending request; an expired request is denied instead."""
        state = await load_run(self._store, run_id)
        for invocation in state.awaiting_approval:
            request = self._approval_request(state, invocation)
            if request.binding != decision.binding:
                continue
            if self._clock() >= request.expires_at:
                return await self._resolve(state, invocation, request, False, "hollis", "expired")
            return await self._resolve(
                state, invocation, request, decision.approved, decision.approver, decision.reason
            )
        raise ApprovalMismatch("no pending invocation matches this approval binding")

    # --- the loop -------------------------------------------------------------------------------

    async def _advance(
        self, state: RunState, provider: ModelProvider, tools: dict[str, Tool]
    ) -> RunState:
        match state.status:
            case RunStatus.MODEL_RESPONDED:
                return await self._after_response(state)
            case RunStatus.TOOL_CALLS:
                return await self._tool_phase(state, tools, provider)
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
                return await self._after_failure(state, provider, tools)
            case _:  # PENDING
                return await self._attempt(state, provider, tools)

    async def _after_response(self, state: RunState) -> RunState:
        response = state.response
        if response is None or not response.tool_calls:
            return await self._append(state, state.run_id, RunCompleted())
        ids = [c.call_id for c in response.tool_calls]
        if len(set(ids)) != len(ids) or not all(ids):
            message = "model returned missing or duplicate tool call ids"
            return await self._fail(state, FailureReason.MODEL_PROTOCOL_ERROR, message, None)
        return await self._decide_all(state)

    # --- model calls ----------------------------------------------------------------------------

    async def _after_failure(
        self, state: RunState, provider: ModelProvider, tools: dict[str, Tool]
    ) -> RunState:
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
        return await self._attempt(state, provider, tools)

    async def _attempt(
        self, state: RunState, provider: ModelProvider, tools: dict[str, Tool]
    ) -> RunState:
        remaining = self._remaining(state)
        if remaining is not None and remaining <= 0:
            return await self._fail(state, FailureReason.BUDGET_EXHAUSTED, "deadline reached", None)
        if state.status is not RunStatus.ATTEMPT_FAILED and (
            len(state.turns) >= state.budget.max_model_turns
        ):
            message = f"max_model_turns {state.budget.max_model_turns} reached"
            return await self._fail(state, FailureReason.BUDGET_EXHAUSTED, message, None)
        timeout_s = state.budget.attempt_timeout_s
        if remaining is not None:
            timeout_s = min(timeout_s, remaining)
        specs = tuple(tools[ref.name].descriptor.spec for ref in state.tools)

        if state.status is RunStatus.ATTEMPT_FAILED and state.operation_id is not None:
            operation_id = state.operation_id
            attempt = state.attempts + 1
        else:
            operation_id = operation_id_for(state.run_id, len(state.turns))
            attempt = 1
            state = await self._prepare_context(state, provider, operation_id, specs)
            if state.status.is_terminal:
                return state
        intent = ModelCallStarted(operation_id=operation_id, attempt=attempt)
        state = await self._append(state, state.run_id, intent)
        context = state.context
        recorded = context if context is not None and context.operation_id == operation_id else None
        request = assembly.build_request(state, operation_id, specs, timeout_s, recorded)
        try:
            async with asyncio.timeout(timeout_s):
                response = await provider.generate(request)
        except ModelCallError as exc:
            return await self._record_failure(state, ProviderFailure.from_error(exc))
        except TimeoutError:
            failure = ProviderFailure(
                kind=FailureKind.TIMEOUT, message=f"no response within {timeout_s:.1f}s"
            )
            return await self._record_failure(state, failure)
        # Any other exception propagates: the run stays in flight and resume records it as
        # an interrupted (ambiguous) attempt.
        recorded = ModelResponseRecorded(
            operation_id=operation_id, attempt=state.attempts, response=self._redact(response)
        )
        return await self._append(state, state.run_id, recorded)

    def _redact(self, response: ModelResponse) -> ModelResponse:
        """Remove registered secrets from everything persisted from the model."""
        r = self._redactor
        calls = tuple(
            call.model_copy(
                update={
                    "arguments": None if call.arguments is None else redact_json(call.arguments, r),
                    "raw_arguments": None
                    if call.raw_arguments is None
                    else r.redact_known(call.raw_arguments),
                }
            )
            for call in response.tool_calls
        )
        continuation = response.continuation
        if continuation is not None:
            continuation = continuation.model_copy(
                update={"data": redact_json(continuation.data, r)}
            )
        return response.model_copy(
            update={
                "content": r.redact_known(response.content),
                "tool_calls": calls,
                "continuation": continuation,
            }
        )

    # --- context --------------------------------------------------------------------------------

    async def _prepare_context(
        self,
        state: RunState,
        provider: ModelProvider,
        operation_id: str,
        specs: tuple[ToolSpec, ...],
    ) -> RunState:
        """Recall, compact if needed, and record the context plan for a new logical call."""
        context = state.context
        if context is not None and context.operation_id == operation_id:
            return state  # recorded before a crash; reuse it unchanged
        state = await self._recall(state)
        capabilities = provider.descriptor.capabilities
        factor = assembly.calibration(state)
        planned = assembly.plan(state, operation_id, specs, capabilities, factor=factor)
        for _ in range(state.context_policy.max_compactions + 1):
            if planned.compact_through is None:
                break
            state = await self._compact(state, planned.compact_through)
            planned = assembly.plan(state, operation_id, specs, capabilities, factor=factor)
        if planned.compact_through is not None:  # unreachable: compactions are bounded
            planned = assembly.plan(
                state, operation_id, specs, capabilities, factor=factor, can_compact=False
            )
        planned = await self._verify_count(state, provider, planned, specs)
        if planned.overflow:
            estimate, limit = planned.estimate.input_tokens, planned.budget.input_tokens
            message = (
                f"mandatory context needs about {estimate} input tokens "
                f"({planned.estimate.accounting.value}); the input budget is {limit}"
            )
            return await self._fail(state, FailureReason.CONTEXT_EXHAUSTED, message, None)
        for note in planned.notes:
            log.info("run %s: %s", state.run_id, note)
        return await self._append(state, state.run_id, planned.record())

    async def _verify_count(
        self,
        state: RunState,
        provider: ModelProvider,
        planned: assembly.ContextPlan,
        specs: tuple[ToolSpec, ...],
    ) -> assembly.ContextPlan:
        """Replace the estimate with the provider's exact count when it is close to the limit."""
        limit = planned.budget.input_tokens
        threshold = state.context_policy.verify_with_provider_at
        if (
            limit is None
            or threshold is None
            or planned.estimate.input_tokens < limit * threshold
            or not isinstance(provider, TokenCounter)
        ):
            return planned
        request = assembly.build_request(
            state, planned.operation_id, specs, state.budget.attempt_timeout_s, planned.record()
        )
        try:
            counted = await provider.count_input_tokens(request)
        except Exception as exc:  # counting is an optimization; the estimate stands
            log.warning("run %s: token counting failed (%s)", state.run_id, type(exc).__name__)
            return planned
        return assembly.with_count(planned, counted)

    async def _recall(self, state: RunState) -> RunState:
        access = state.memory_access
        if (
            self._memory is None
            or access is None
            or not access.read
            or state.memory_recalled
            or state.turns
            or state.context_policy.memory_limit == 0
        ):
            return state
        query = RecallQuery(
            text=state.input[:2000],
            scopes=access.read,
            limit=state.context_policy.memory_limit,
            as_of=self._clock(),
            max_chars=state.context_policy.memory_chars or None,
        )
        recalled = await self._memory.recall(query)
        memories = tuple(
            m.model_copy(update={"content": self._redactor.redact_known(m.content)})
            for m in recalled
            if m.scope in access.read  # defence in depth: never trust the backend's filter alone
        )
        log.info("run %s: recalled %d memories", state.run_id, len(memories))
        event = MemoryRecalled(query=query.text, scopes=access.read, memories=memories)
        return await self._append(state, state.run_id, event)

    async def _compact(self, state: RunState, through: int) -> RunState:
        previous = state.compactions[-1] if state.compactions else None
        start = state.compacted_through
        last = next(
            (
                t.response.content
                for t in reversed(state.turns[start:through])
                if t.response.content
            ),
            "",
        )
        request = SummarizeRequest(
            run_id=state.run_id,
            compaction=len(state.compactions),
            from_turn=start,
            through_turn=through,
            objective=state.input[:4000],
            previous=previous,
            transcript=assembly.transcript(state, start, through),
            last_assistant=last,
        )
        method = self._summarizer.method
        try:
            draft = await self._summarizer.summarize(request)
            summary = merge_summary(previous, draft, through_turn=through, method=method)
        except Exception as exc:  # any summarizer failure falls back; nothing from it is used
            reason = str(exc) if isinstance(exc, SummaryRejected) else type(exc).__name__
            log.warning("run %s: summarizer failed (%s); using fallback", state.run_id, reason)
            summary = merge_summary(
                previous,
                deterministic_draft(request),
                through_turn=through,
                method=CompactionMethod.DETERMINISTIC_FALLBACK,
                fallback_reason=self._redactor.redact(reason)[:500],
            )
        summary = summary.model_copy(
            update={
                "state": self._redactor.redact_known(summary.state),
                "items": tuple(
                    i.model_copy(update={"text": self._redactor.redact_known(i.text)})
                    for i in summary.items
                ),
            }
        )
        log.info(
            "run %s: compacted turns %d-%d (%s)", state.run_id, start, through - 1, summary.method
        )
        event = ContextCompacted(compaction=len(state.compactions), summary=summary)
        return await self._append(state, state.run_id, event)

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
            operation_id=self._operation(state), attempt=state.attempts, failure=failure
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
            operation_id=self._operation(state),
            reason=reason,
            message=message,
            kind=failure.kind if failure else None,
        )
        return await self._append(state, state.run_id, payload)

    def _operation(self, state: RunState) -> str:
        return state.operation_id or operation_id_for(state.run_id, len(state.turns))

    # --- tool phase: decisions ------------------------------------------------------------------

    async def _decide_all(self, state: RunState) -> RunState:
        turn = len(state.turns) - 1
        for invocation in state.turns[-1].invocations:
            if invocation.decision is not None:
                continue
            decision, reason, rule, arguments = self._decide(state, invocation)
            log.info(
                "run %s: tool call %s (%s) -> %s",
                state.run_id,
                invocation.invocation_id,
                invocation.call.name,
                decision,
            )
            state = await self._append(
                state,
                state.run_id,
                ToolCallDecided(
                    invocation_id=invocation.invocation_id,
                    turn=turn,
                    index=invocation.index,
                    call_id=invocation.call.call_id,
                    tool=invocation.call.name,
                    decision=decision,
                    reason=self._redactor.redact(reason),
                    rule=rule,
                    arguments=arguments,
                ),
            )
        return state

    def _decide(
        self, state: RunState, invocation: Invocation
    ) -> tuple[Decision, str, str | None, dict[str, JsonValue] | None]:
        call = invocation.call
        ref = state.tool(call.name)
        if ref is None or self._tools is None:
            available = ", ".join(t.name for t in state.tools) or "none"
            return (
                Decision.INVALID,
                f"unknown tool {call.name!r}; available: {available}",
                None,
                None,
            )
        if call.arguments is None:
            detail = call.parse_error or "not a JSON object"
            return Decision.INVALID, f"arguments are not a valid JSON object ({detail})", None, None
        if _contains_redacted(call.arguments):
            return (
                Decision.DENY,
                "the arguments contained a credential; this call is refused",
                "credential-guard",
                None,
            )
        authorized_this_turn = sum(1 for i in state.turns[-1].invocations if _counts(i))
        if state.tool_calls_authorized >= state.budget.max_tool_calls:
            return Decision.INVALID, "the run's tool call limit has been reached", "limits", None
        if authorized_this_turn >= state.budget.max_tool_calls_per_turn:
            limit = state.budget.max_tool_calls_per_turn
            return Decision.INVALID, f"at most {limit} tool calls per turn", "limits", None
        tool = self._tools.get(call.name)
        if tool is None:  # verified at execute(); defensive
            return Decision.INVALID, f"tool {call.name!r} is unavailable", None, None
        try:
            arguments = tool.validate(call.arguments)
        except ToolArgumentError as exc:
            return Decision.INVALID, f"invalid arguments: {exc}", None, None
        verdict = self._policy.decide(
            PolicyRequest(
                run_id=state.run_id,
                invocation_id=invocation.invocation_id,
                tool=ref,
                arguments=arguments,
                environment=self._environment,
            )
        )
        return verdict.decision, verdict.reason, verdict.rule, arguments

    # --- tool phase: approvals and execution ----------------------------------------------------

    async def _tool_phase(
        self, state: RunState, tools: dict[str, Tool], provider: ModelProvider
    ) -> RunState:
        if any(i.decision is None for i in state.turns[-1].invocations):
            return await self._decide_all(state)
        if state.awaiting_approval:
            return await self._seek_approvals(state)
        pending = state.pending_invocations
        remaining = self._remaining(state)
        if (
            pending
            and remaining is not None
            and remaining <= 0
            and any(i.authorized for i in pending)
        ):
            # Never start a tool once the deadline has passed (it could only half-run).
            message = "deadline reached before tool execution"
            return await self._fail(state, FailureReason.BUDGET_EXHAUSTED, message, None)
        if pending:
            writer = _Writer(self, state)
            for invocation in pending:
                if not invocation.authorized:
                    await writer.append(_not_executed(invocation))
            await self._execute(writer, tools)
            return writer.state
        return await self._attempt(state, provider, tools)

    async def _seek_approvals(self, state: RunState) -> RunState:
        for invocation in state.awaiting_approval:
            request = self._approval_request(state, invocation)
            if self._clock() >= request.expires_at:
                state = await self._resolve(state, invocation, request, False, "hollis", "expired")
            elif self._approver is None:
                reason = "approval required but no approver is configured"
                state = await self._resolve(state, invocation, request, False, "hollis", reason)
            else:
                decision = await self._approver.review(request)
                if decision is None:
                    continue  # stays pending; the run suspends
                if decision.binding != request.binding:
                    reason = "approval was for a different invocation"
                    state = await self._resolve(state, invocation, request, False, "hollis", reason)
                else:
                    state = await self._resolve(
                        state,
                        invocation,
                        request,
                        decision.approved,
                        decision.approver,
                        decision.reason,
                    )
        return state

    def _approval_request(self, state: RunState, invocation: Invocation) -> ApprovalRequest:
        ref = state.tool(invocation.call.name)
        if ref is None or invocation.arguments is None or invocation.decided_at is None:
            raise AssertionError("approval requested for an unauthorized call")
        return ApprovalRequest(
            run_id=state.run_id,
            invocation_id=invocation.invocation_id,
            tool=ref,
            arguments=invocation.arguments,
            reason=invocation.reason or "",
            binding=approval_binding(
                state.run_id, invocation.invocation_id, ref, invocation.arguments
            ),
            expires_at=invocation.decided_at + timedelta(seconds=state.budget.approval_timeout_s),
        )

    async def _resolve(
        self,
        state: RunState,
        invocation: Invocation,
        request: ApprovalRequest,
        approved: bool,
        approver: str,
        reason: str | None,
    ) -> RunState:
        log.info(
            "run %s: approval for %s %s by %s",
            state.run_id,
            invocation.invocation_id,
            "granted" if approved else "refused",
            approver,
        )
        return await self._append(
            state,
            state.run_id,
            ToolApprovalResolved(
                invocation_id=invocation.invocation_id,
                binding=request.binding,
                approved=approved,
                approver=approver,
                reason=self._redactor.redact(reason) if reason else None,
            ),
        )

    async def _execute(self, writer: _Writer, tools: dict[str, Tool]) -> None:
        state = writer.state
        runnable = [i for i in state.pending_invocations if i.authorized]
        interrupted = [i for i in runnable if i.attempts > 0]
        fresh = [i for i in runnable if i.attempts == 0]
        for invocation in interrupted:
            await self._recover(writer, invocation, tools[invocation.call.name])
        parallel = len(fresh) > 1 and all(
            tools[i.call.name].descriptor.effect is ToolEffect.READ_ONLY for i in fresh
        )
        if parallel:
            limit = asyncio.Semaphore(state.budget.max_parallel_tools)

            async def bounded(invocation: Invocation) -> None:
                async with limit:
                    await self._run_tool(writer, invocation, tools[invocation.call.name], 1)

            async with asyncio.TaskGroup() as group:
                for invocation in fresh:
                    group.create_task(bounded(invocation))
        else:
            for invocation in fresh:  # side effects run one at a time, in the model's order
                await self._run_tool(writer, invocation, tools[invocation.call.name], 1)

    async def _recover(self, writer: _Writer, invocation: Invocation, tool: Tool) -> None:
        descriptor = tool.descriptor
        if descriptor.idempotent:
            await self._run_tool(writer, invocation, tool, invocation.attempts + 1)
            return
        context = self._context(writer.state, invocation, invocation.attempts)
        if descriptor.reconcilable:
            try:
                output = await tool.reconcile(context)
            except Exception as exc:  # reconciliation is best effort
                log.warning(
                    "reconcile failed for %s: %s", invocation.invocation_id, type(exc).__name__
                )
                output = None
            if output is not None:
                outcome = ToolOutcome.ERROR if output.is_error else ToolOutcome.SUCCESS
                await self._record_result(writer, invocation, outcome, output, None)
                return
        message = (
            f"the outcome of {descriptor.name} is unknown: execution was interrupted and the tool "
            "is not idempotent, so it was not run again. Check the external system before "
            "retrying."
        )
        await self._record_result(
            writer, invocation, ToolOutcome.AMBIGUOUS, ToolOutput.text(message, is_error=True), None
        )

    async def _run_tool(
        self, writer: _Writer, invocation: Invocation, tool: Tool, attempt: int
    ) -> None:
        await writer.append(
            ToolExecutionStarted(invocation_id=invocation.invocation_id, attempt=attempt)
        )
        descriptor = tool.descriptor
        timeout_s = descriptor.timeout_s
        remaining = self._remaining(writer.state)
        if remaining is not None:
            timeout_s = max(0.001, min(timeout_s, remaining))
        context = self._context(writer.state, invocation, attempt)
        arguments: dict[str, Any] = dict(invocation.arguments or {})
        read_only = descriptor.effect is ToolEffect.READ_ONLY
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_s):
                output = await tool.invoke(arguments, context)
            outcome = ToolOutcome.ERROR if output.is_error else ToolOutcome.SUCCESS
        except ToolError as exc:
            output, outcome = ToolOutput.text(str(exc), is_error=True), ToolOutcome.ERROR
        except ToolArgumentError as exc:
            output, outcome = (
                ToolOutput.text(f"invalid arguments: {exc}", is_error=True),
                ToolOutcome.ERROR,
            )
        except TimeoutError:
            outcome = ToolOutcome.TIMEOUT if read_only else ToolOutcome.AMBIGUOUS
            effect = "" if read_only else " and may or may not have taken effect"
            output = ToolOutput.text(
                f"{descriptor.name} did not finish within {timeout_s:.1f}s{effect}", is_error=True
            )
        except Exception as exc:  # a tool bug must not corrupt the run
            log.warning("tool %s raised %s", descriptor.name, type(exc).__name__)
            outcome = ToolOutcome.ERROR if read_only else ToolOutcome.AMBIGUOUS
            output = ToolOutput.text(
                f"{descriptor.name} failed unexpectedly ({type(exc).__name__})", is_error=True
            )
        duration_ms = int((time.monotonic() - started) * 1000)
        await self._record_result(writer, invocation, outcome, output, duration_ms)

    async def _record_result(
        self,
        writer: _Writer,
        invocation: Invocation,
        outcome: ToolOutcome,
        output: ToolOutput,
        duration_ms: int | None,
    ) -> None:
        state = writer.state
        parts, truncated, original = finalize(
            output, limit=state.budget.max_tool_output_chars, redactor=self._redactor
        )
        artifact: ArtifactRef | None = None
        policy = state.context_policy
        if self._artifacts is not None and original > min(
            policy.artifact_threshold_chars, state.budget.max_tool_output_chars
        ):
            full = render_parts(redact_parts(output.parts, self._redactor))
            ref = state.tool(invocation.call.name)
            try:
                artifact = await self._artifacts.put(
                    state.run_id,
                    full,
                    source=invocation.invocation_id,
                    trust=ref.trust if ref is not None else ToolTrust.EXTERNAL,
                )
            except Exception as exc:  # storage failure must not lose the result: truncate
                log.warning("artifact store failed (%s); truncating", type(exc).__name__)
            else:
                limit = min(policy.artifact_preview_chars, state.budget.max_tool_output_chars)
                parts, truncated = (artifact_preview(full, artifact, limit),), True
        log.info(
            "run %s: tool call %s finished: %s (%s ms%s)",
            writer.state.run_id,
            invocation.invocation_id,
            outcome,
            duration_ms,
            ", truncated" if truncated else "",
        )
        await writer.append(
            ToolResultRecorded(
                invocation_id=invocation.invocation_id,
                outcome=outcome,
                parts=parts,
                truncated=truncated,
                original_chars=original,
                duration_ms=duration_ms,
                artifact=artifact,
            )
        )

    def _context(self, state: RunState, invocation: Invocation, attempt: int) -> ToolContext:
        return ToolContext(
            run_id=state.run_id,
            invocation_id=invocation.invocation_id,
            attempt=max(attempt, 1),
            deadline=state.deadline,
            memory=state.memory_access,
        )

    # --- helpers --------------------------------------------------------------------------------

    def _require_tool(self, name: str) -> Tool:
        tool = self._tools.get(name) if self._tools is not None else None
        if tool is None:
            raise UnknownTool(f"tool {name!r} is not in the catalog")
        return tool

    def _verify_tools(self, state: RunState) -> dict[str, Tool]:
        tools: dict[str, Tool] = {}
        for ref in state.tools:
            tool = self._tools.get(ref.name) if self._tools is not None else None
            if tool is None:
                raise ToolChanged(f"run {state.run_id}: tool {ref.name!r} is no longer available")
            if tool.descriptor.ref != ref:
                raise ToolChanged(
                    f"run {state.run_id}: tool {ref.name!r} changed since the run began"
                )
            tools[ref.name] = tool
        return tools

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


def _counts(invocation: Invocation) -> bool:
    return invocation.decision in (Decision.ALLOW, Decision.REQUIRE_APPROVAL)


def _contains_redacted(value: JsonValue) -> bool:
    if isinstance(value, str):
        return REDACTED in value
    if isinstance(value, list):
        return any(_contains_redacted(v) for v in value)
    if isinstance(value, dict):
        return any(REDACTED in k or _contains_redacted(v) for k, v in value.items())
    return False


def _not_executed(invocation: Invocation) -> ToolResultRecorded:
    """The result for a call that was rejected, denied or refused approval."""
    if invocation.decision is Decision.INVALID:
        outcome, prefix = ToolOutcome.INVALID, "tool call rejected"
    elif invocation.decision is Decision.DENY:
        outcome, prefix = ToolOutcome.DENIED, "tool call denied by policy"
    else:
        outcome, prefix = ToolOutcome.DENIED, "tool call not approved"
    detail = invocation.approval_reason if invocation.approved is False else invocation.reason
    text = f"{prefix}: {detail}" if detail else prefix
    return ToolResultRecorded(
        invocation_id=invocation.invocation_id,
        outcome=outcome,
        parts=ToolOutput.text(text, is_error=True).parts,
        original_chars=len(text),
    )
