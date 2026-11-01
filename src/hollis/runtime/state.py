"""Run status, the legal transition table, and state reconstruction by folding events.

The fold is the enforcement point for run semantics (ADR-0005, ADR-0007, ADR-0014): whatever code
appends events, a log that violates these rules cannot be folded. In particular, a tool can only
start executing after an allow decision or a matching recorded approval, nothing in a turn runs
while any approval in it is pending, and a started invocation can only be retried if its tool is
declared idempotent.

Context rules (ADR-0018): memories are recalled at most once, before the first model call, and only
from namespaces granted to the run; a compaction covers whole turns and always leaves the latest
turn verbatim; and an assembled context must start exactly where the latest compaction ends, so no
turn can be dropped from a request without being represented by a recorded summary.
"""

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from hollis.runtime.artifacts import ArtifactRef
from hollis.runtime.context import CompactionSummary, ContextPolicy, TokenEstimate
from hollis.runtime.conversation import ToolCall
from hollis.runtime.delegation import Assignment, DelegationLimits, Lineage
from hollis.runtime.events import (
    AnswerGrounded,
    ContextAssembled,
    ContextCompacted,
    EventEnvelope,
    EventType,
    FailureReason,
    KnowledgeRetrieved,
    MemoryRecalled,
    ModelCallFailed,
    ModelCallStarted,
    ModelResponseRecorded,
    ProviderFailure,
    RunCancelled,
    RunCompleted,
    RunCreated,
    RunFailed,
    ToolApprovalResolved,
    ToolCallDecided,
    ToolExecutionStarted,
    ToolOutcome,
    ToolResultRecorded,
)
from hollis.runtime.grounding import GroundingReport
from hollis.runtime.knowledge import KnowledgeAccess, RetrievedPassage
from hollis.runtime.memory import MemoryAccess, RecalledMemory
from hollis.runtime.model import CallOutcome, DeploymentRef, FailureKind, ModelResponse
from hollis.runtime.policy import AmbiguousOutcomePolicy, RunBudget
from hollis.runtime.tools import (
    Decision,
    ResultPart,
    ToolRef,
    approval_binding,
    invocation_id_for,
)


class RunStatus(StrEnum):
    PENDING = "pending"
    MODEL_CALL_IN_FLIGHT = "model_call_in_flight"
    """An attempt's intent is recorded and its outcome is not."""
    ATTEMPT_FAILED = "attempt_failed"
    """The latest attempt has a recorded failure; the runner decides whether to retry."""
    MODEL_RESPONDED = "model_responded"
    TOOL_CALLS = "tool_calls"
    """The latest response's tool calls are being decided, approved or executed."""
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED)


class InvalidEventSequence(Exception):
    """An event log (or a single appended event) violates run semantics."""


class IllegalTransition(InvalidEventSequence):
    def __init__(self, current: RunStatus | None, event_type: EventType) -> None:
        self.current = current
        self.event_type = event_type
        super().__init__(f"illegal transition: {event_type} in status {current}")


S = RunStatus
E = EventType
_TOOL_EVENTS = (
    E.TOOL_CALL_DECIDED,
    E.TOOL_APPROVAL_RESOLVED,
    E.TOOL_EXECUTION_STARTED,
    E.TOOL_RESULT_RECORDED,
)

# (current status, event type) -> next status. `None` means "no run yet". Anything absent is
# illegal. Finer rules (which invocation, which turn) are checked in `apply`.
TRANSITIONS: Mapping[tuple[RunStatus | None, EventType], RunStatus] = {
    (None, E.RUN_CREATED): S.PENDING,
    (S.PENDING, E.MODEL_CALL_STARTED): S.MODEL_CALL_IN_FLIGHT,
    (S.PENDING, E.RUN_FAILED): S.FAILED,
    (S.PENDING, E.MEMORY_RECALLED): S.PENDING,
    (S.PENDING, E.CONTEXT_ASSEMBLED): S.PENDING,
    (S.PENDING, E.KNOWLEDGE_RETRIEVED): S.PENDING,
    (S.MODEL_CALL_IN_FLIGHT, E.MODEL_RESPONSE_RECORDED): S.MODEL_RESPONDED,
    (S.MODEL_CALL_IN_FLIGHT, E.MODEL_CALL_FAILED): S.ATTEMPT_FAILED,
    (S.ATTEMPT_FAILED, E.MODEL_CALL_STARTED): S.MODEL_CALL_IN_FLIGHT,
    (S.ATTEMPT_FAILED, E.RUN_FAILED): S.FAILED,
    (S.MODEL_RESPONDED, E.RUN_COMPLETED): S.COMPLETED,
    (S.MODEL_RESPONDED, E.TOOL_CALL_DECIDED): S.TOOL_CALLS,
    (S.MODEL_RESPONDED, E.RUN_FAILED): S.FAILED,
    (S.MODEL_RESPONDED, E.ANSWER_GROUNDED): S.MODEL_RESPONDED,
    **{(S.TOOL_CALLS, event): S.TOOL_CALLS for event in _TOOL_EVENTS},
    (S.TOOL_CALLS, E.CONTEXT_COMPACTED): S.TOOL_CALLS,
    (S.TOOL_CALLS, E.CONTEXT_ASSEMBLED): S.TOOL_CALLS,
    (S.TOOL_CALLS, E.MODEL_CALL_STARTED): S.MODEL_CALL_IN_FLIGHT,
    (S.TOOL_CALLS, E.RUN_FAILED): S.FAILED,
    **{
        (status, E.RUN_CANCELLED): S.CANCELLED
        for status in (
            S.PENDING,
            S.MODEL_CALL_IN_FLIGHT,
            S.ATTEMPT_FAILED,
            S.MODEL_RESPONDED,
            S.TOOL_CALLS,
        )
    },
}


def next_status(current: RunStatus | None, event_type: EventType) -> RunStatus:
    try:
        return TRANSITIONS[(current, event_type)]
    except KeyError:
        raise IllegalTransition(current, event_type) from None


def operation_id_for(run_id: UUID, call_index: int = 0) -> str:
    """Stable idempotency key for a run's logical model call (one per turn)."""
    return f"{run_id}/model/{call_index}"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RunFailure(_Frozen):
    reason: FailureReason
    message: str
    kind: FailureKind | None = None


class RunCancellation(_Frozen):
    reason: str
    by: str
    interrupted: tuple[str, ...] = ()


class UsageTotals(_Frozen):
    """Provider-reported usage summed over every recorded response of the run."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class Invocation(_Frozen):
    """One model tool call and everything decided and done about it."""

    invocation_id: str
    index: int
    call: ToolCall
    decision: Decision | None = None
    reason: str | None = None
    rule: str | None = None
    arguments: dict[str, JsonValue] | None = None
    decided_at: AwareDatetime | None = None
    approved: bool | None = None
    approver: str | None = None
    approval_reason: str | None = None
    attempts: int = 0
    outcome: ToolOutcome | None = None
    parts: tuple[ResultPart, ...] = ()
    truncated: bool = False
    original_chars: int = 0
    artifact: ArtifactRef | None = None

    @property
    def awaiting_approval(self) -> bool:
        return self.decision is Decision.REQUIRE_APPROVAL and self.approved is None

    @property
    def authorized(self) -> bool:
        return self.decision is Decision.ALLOW or (
            self.decision is Decision.REQUIRE_APPROVAL and self.approved is True
        )

    @property
    def finished(self) -> bool:
        return self.outcome is not None


class Turn(_Frozen):
    response: ModelResponse
    invocations: tuple[Invocation, ...] = ()
    estimate: TokenEstimate | None = None
    """The input estimate recorded for the request that produced this turn, if any."""


class RunState(_Frozen):
    """A run's state, derived entirely from its event log. Never mutated in place."""

    run_id: UUID
    version: int = Field(ge=1)
    status: RunStatus
    created_at: AwareDatetime
    model: str
    input: str
    instructions: str | None
    deployment: DeploymentRef
    budget: RunBudget
    ambiguous_policy: AmbiguousOutcomePolicy
    tools: tuple[ToolRef, ...] = ()
    turns: tuple[Turn, ...] = ()
    operation_id: str | None = None
    attempts: int = Field(default=0, ge=0)
    """Attempts of the current logical model call."""
    last_failure: ProviderFailure | None = None
    response: ModelResponse | None = None
    """The latest recorded model response."""
    failure: RunFailure | None = None
    cancellation: RunCancellation | None = None
    usage: UsageTotals = UsageTotals()
    tool_calls_authorized: int = 0
    """Invocations decided as allow or require-approval (counted against the budget)."""
    context_policy: ContextPolicy = ContextPolicy()
    memory_access: MemoryAccess | None = None
    memory_recalled: bool = False
    memories: tuple[RecalledMemory, ...] = ()
    """Memories recalled at run start, in ranked order."""
    compactions: tuple[CompactionSummary, ...] = ()
    knowledge_access: KnowledgeAccess | None = None
    knowledge_retrieved: bool = False
    passages: tuple[RetrievedPassage, ...] = ()
    """Knowledge passages retrieved at run start, in rank order."""
    grounding: GroundingReport | None = None
    """The grounding check of the latest final answer, if one was recorded."""
    context: ContextAssembled | None = None
    """The assembled context of the current (or most recent) logical model call."""
    lineage: Lineage | None = None
    assignment: Assignment | None = None
    delegation: DelegationLimits | None = None

    @property
    def depth(self) -> int:
        """Delegation depth: 0 for a run nobody delegated."""
        return self.lineage.depth if self.lineage is not None else 0

    @property
    def total_tokens(self) -> int:
        return self.usage.input_tokens + self.usage.output_tokens

    @property
    def token_budget_reached(self) -> bool:
        limit = self.budget.max_total_tokens
        return limit is not None and self.total_tokens >= limit

    @property
    def compacted_through(self) -> int:
        """Turns before this index are represented by the latest summary."""
        return self.compactions[-1].through_turn if self.compactions else 0

    @property
    def deadline(self) -> datetime | None:
        if self.budget.deadline_s is None:
            return None
        return self.created_at + timedelta(seconds=self.budget.deadline_s)

    def tool(self, name: str) -> ToolRef | None:
        return next((t for t in self.tools if t.name == name), None)

    @property
    def pending_invocations(self) -> tuple[Invocation, ...]:
        """Invocations of the latest turn that have no result yet."""
        if not self.turns:
            return ()
        return tuple(i for i in self.turns[-1].invocations if not i.finished)

    @property
    def awaiting_approval(self) -> tuple[Invocation, ...]:
        """Invocations waiting for a decision. A finished run (for example one cancelled while
        waiting) waits for nothing."""
        if self.status.is_terminal:
            return ()
        return tuple(i for i in self.pending_invocations if i.awaiting_approval)


def apply(state: RunState | None, event: EventEnvelope) -> RunState:
    """Return the state after `event`, or raise InvalidEventSequence. Pure."""
    expected_sequence = 1 if state is None else state.version + 1
    if event.sequence != expected_sequence:
        raise InvalidEventSequence(f"expected sequence {expected_sequence}, got {event.sequence}")
    if state is not None and event.run_id != state.run_id:
        raise InvalidEventSequence(f"event for run {event.run_id} in log of run {state.run_id}")

    status = next_status(state.status if state else None, event.event_type)
    payload = event.payload
    if isinstance(payload, RunCreated):
        names = [t.name for t in payload.tools]
        if len(set(names)) != len(names):
            raise InvalidEventSequence("duplicate tool names in RunCreated")
        _check_lineage(event.run_id, payload)
        return RunState(
            run_id=event.run_id,
            version=event.sequence,
            status=status,
            created_at=event.occurred_at,
            model=payload.model,
            input=payload.input,
            instructions=payload.instructions,
            deployment=payload.deployment,
            budget=payload.budget,
            ambiguous_policy=payload.ambiguous_policy,
            tools=payload.tools,
            context_policy=payload.context,
            memory_access=payload.memory,
            knowledge_access=payload.knowledge,
            lineage=payload.lineage,
            assignment=payload.assignment,
            delegation=payload.delegation,
        )

    if state is None:  # unreachable: next_status only allows RunCreated without a run
        raise IllegalTransition(None, event.event_type)
    update: dict[str, Any] = {"version": event.sequence, "status": status}
    match payload:
        case ModelCallStarted(operation_id=op, attempt=attempt):
            update |= _model_call_started(state, op, attempt)
        case ModelResponseRecorded(operation_id=op, attempt=attempt, response=response):
            _require_current_attempt(state, op, attempt)
            update["grounding"] = None
            turn_index = len(state.turns)
            invocations = tuple(
                Invocation(
                    invocation_id=invocation_id_for(state.run_id, turn_index, i),
                    index=i,
                    call=call,
                )
                for i, call in enumerate(response.tool_calls)
            )
            context = state.context
            estimate = context.estimate if context and context.operation_id == op else None
            update |= {
                "response": response,
                "turns": (
                    *state.turns,
                    Turn(response=response, invocations=invocations, estimate=estimate),
                ),
            }
            if response.usage is not None:
                update["usage"] = UsageTotals(
                    input_tokens=state.usage.input_tokens + response.usage.input_tokens,
                    output_tokens=state.usage.output_tokens + response.usage.output_tokens,
                )
        case ModelCallFailed(operation_id=op, attempt=attempt, failure=failure):
            _require_current_attempt(state, op, attempt)
            update["last_failure"] = failure
        case ToolCallDecided():
            update |= _tool_call_decided(state, payload, event.occurred_at)
        case ToolApprovalResolved():
            update |= _tool_approval_resolved(state, payload)
        case ToolExecutionStarted():
            update |= _tool_execution_started(state, payload)
        case ToolResultRecorded():
            update |= _tool_result_recorded(state, payload)
        case MemoryRecalled():
            update |= _memory_recalled(state, payload)
        case ContextCompacted():
            update |= _context_compacted(state, payload)
        case ContextAssembled():
            update |= _context_assembled(state, payload)
        case KnowledgeRetrieved():
            update |= _knowledge_retrieved(state, payload)
        case AnswerGrounded(operation_id=op, report=report):
            if op != state.operation_id or state.grounding is not None:
                raise InvalidEventSequence("grounding is recorded once, for the current response")
            if state.response is None or state.response.tool_calls:
                raise InvalidEventSequence("grounding applies to a final answer")
            update["grounding"] = report
        case RunFailed(operation_id=op, reason=reason, message=message, kind=kind):
            if state.operation_id is not None and op != state.operation_id:
                raise InvalidEventSequence(f"operation_id {op!r} != {state.operation_id!r}")
            _require_consistent_reason(state, reason)
            update["failure"] = RunFailure(reason=reason, message=message, kind=kind)
        case RunCompleted():
            if state.response is not None and state.response.tool_calls:
                raise InvalidEventSequence("cannot complete with unanswered tool calls")
        case RunCancelled(reason=reason, by=by, interrupted=interrupted):
            running = {i.invocation_id for i in state.pending_invocations if i.attempts > 0}
            if not set(interrupted) <= running:
                raise InvalidEventSequence("cancellation names invocations that were not running")
            update["cancellation"] = RunCancellation(reason=reason, by=by, interrupted=interrupted)
    return state.model_copy(update=update)


def fold(events: Iterable[EventEnvelope]) -> RunState:
    """Reconstruct a run's state from its complete event log. Pure."""
    state: RunState | None = None
    seen: set[UUID] = set()
    for event in events:
        if event.event_id in seen:
            raise InvalidEventSequence(f"duplicate event_id {event.event_id}")
        seen.add(event.event_id)
        state = apply(state, event)
    if state is None:
        raise InvalidEventSequence("empty event log")
    return state


# --- model calls --------------------------------------------------------------------------------


def _check_lineage(run_id: UUID, created: RunCreated) -> None:
    lineage = created.lineage
    if lineage is None:
        if created.assignment is not None:
            raise InvalidEventSequence("an assignment needs a parent run")
        return
    if run_id in (lineage.parent_run_id, lineage.root_run_id):
        raise InvalidEventSequence("a run cannot be its own parent or root")
    if lineage.depth == 1 and lineage.root_run_id != lineage.parent_run_id:
        raise InvalidEventSequence("a depth-1 run's parent is its root")
    if created.delegation is not None and lineage.depth > created.delegation.max_depth:
        raise InvalidEventSequence(
            f"depth {lineage.depth} exceeds max_depth {created.delegation.max_depth}"
        )


def _model_call_started(state: RunState, op: str, attempt: int) -> dict[str, Any]:
    if state.token_budget_reached:
        raise InvalidEventSequence(f"max_total_tokens {state.budget.max_total_tokens} reached")
    if state.status is RunStatus.ATTEMPT_FAILED:
        if op != state.operation_id:
            raise InvalidEventSequence(f"operation_id {op!r} != {state.operation_id!r}")
        if attempt != state.attempts + 1:
            raise InvalidEventSequence(f"attempt {attempt} after {state.attempts}")
        if attempt > state.budget.max_attempts:
            raise InvalidEventSequence(
                f"attempt {attempt} exceeds max_attempts {state.budget.max_attempts}"
            )
        return {"attempts": attempt}
    # A new logical call: the run's first, or the next turn after all tool results are in.
    turn = len(state.turns)
    if state.status is RunStatus.TOOL_CALLS and state.pending_invocations:
        raise InvalidEventSequence("next model turn before every tool call has a result")
    if turn >= state.budget.max_model_turns:
        raise InvalidEventSequence(f"turn {turn} exceeds max_model_turns")
    expected = operation_id_for(state.run_id, turn)
    if op != expected:
        raise InvalidEventSequence(f"operation_id {op!r} != {expected!r}")
    if state.context is not None and state.context.operation_id not in (op, _previous_op(state)):
        raise InvalidEventSequence("the assembled context belongs to a different model call")
    if attempt != 1:
        raise InvalidEventSequence(f"a new model call starts at attempt 1, not {attempt}")
    return {"operation_id": op, "attempts": 1, "last_failure": None}


def _require_current_attempt(state: RunState, operation_id: str, attempt: int) -> None:
    if operation_id != state.operation_id:
        raise InvalidEventSequence(f"operation_id {operation_id!r} != {state.operation_id!r}")
    if attempt != state.attempts:
        raise InvalidEventSequence(f"outcome for attempt {attempt}, latest {state.attempts}")


def _require_consistent_reason(state: RunState, reason: FailureReason) -> None:
    if reason is FailureReason.CONTEXT_EXHAUSTED:
        if state.status not in (RunStatus.PENDING, RunStatus.TOOL_CALLS) or (
            state.pending_invocations
        ):
            raise InvalidEventSequence("context_exhausted only before a new model call")
        return
    if state.status in (RunStatus.MODEL_RESPONDED, RunStatus.TOOL_CALLS):
        if reason not in (FailureReason.BUDGET_EXHAUSTED, FailureReason.MODEL_PROTOCOL_ERROR):
            raise InvalidEventSequence(f"{reason} is not a valid failure during tool calls")
        return
    failure = state.last_failure if state.status is RunStatus.ATTEMPT_FAILED else None
    match reason:
        case FailureReason.PROVIDER_ERROR:
            if failure is None or failure.kind.retryable:
                raise InvalidEventSequence("provider_error requires a non-retryable last failure")
        case FailureReason.AMBIGUOUS_OUTCOME:
            if failure is None or failure.outcome is not CallOutcome.AMBIGUOUS:
                raise InvalidEventSequence("ambiguous_outcome requires an ambiguous last failure")
        case FailureReason.MODEL_PROTOCOL_ERROR:
            raise InvalidEventSequence("model_protocol_error requires a recorded response")
        case FailureReason.BUDGET_EXHAUSTED:
            pass


# --- tool calls ---------------------------------------------------------------------------------


def _replace(state: RunState, invocation: Invocation) -> dict[str, Any]:
    last = state.turns[-1]
    invocations = tuple(
        invocation if i.invocation_id == invocation.invocation_id else i for i in last.invocations
    )
    return {"turns": (*state.turns[:-1], last.model_copy(update={"invocations": invocations}))}


def _invocation(state: RunState, invocation_id: str) -> Invocation:
    if state.turns:
        for invocation in state.turns[-1].invocations:
            if invocation.invocation_id == invocation_id:
                return invocation
    raise InvalidEventSequence(f"no tool call {invocation_id!r} in the current turn")


def _tool_call_decided(
    state: RunState, event: ToolCallDecided, occurred_at: datetime
) -> dict[str, Any]:
    if not state.turns or event.turn != len(state.turns) - 1:
        raise InvalidEventSequence(f"decision for turn {event.turn} outside the current turn")
    invocations = state.turns[-1].invocations
    undecided = [i for i in invocations if i.decision is None]
    if not undecided or undecided[0].index != event.index:
        raise InvalidEventSequence(f"tool calls must be decided in order (got {event.index})")
    invocation = undecided[0]
    if event.invocation_id != invocation.invocation_id or event.call_id != invocation.call.call_id:
        raise InvalidEventSequence("decision does not match the model's tool call")
    if event.tool != invocation.call.name:
        raise InvalidEventSequence("decision names a different tool than the model called")
    update: dict[str, Any] = {}
    if event.decision in (Decision.ALLOW, Decision.REQUIRE_APPROVAL):
        if state.tool(event.tool) is None:
            raise InvalidEventSequence(f"tool {event.tool!r} is not enabled for this run")
        if event.arguments is None:
            raise InvalidEventSequence("an authorized call needs validated arguments")
        this_turn = sum(
            1 for i in invocations if i.decision in (Decision.ALLOW, Decision.REQUIRE_APPROVAL)
        )
        if state.tool_calls_authorized >= state.budget.max_tool_calls:
            raise InvalidEventSequence("max_tool_calls exceeded")
        if this_turn >= state.budget.max_tool_calls_per_turn:
            raise InvalidEventSequence("max_tool_calls_per_turn exceeded")
        update["tool_calls_authorized"] = state.tool_calls_authorized + 1
    decided = invocation.model_copy(
        update={
            "decision": event.decision,
            "reason": event.reason,
            "rule": event.rule,
            "arguments": event.arguments,
            "decided_at": occurred_at,
        }
    )
    return update | _replace(state, decided)


def _tool_approval_resolved(state: RunState, event: ToolApprovalResolved) -> dict[str, Any]:
    invocation = _invocation(state, event.invocation_id)
    if not invocation.awaiting_approval:
        raise InvalidEventSequence(f"{event.invocation_id!r} is not awaiting approval")
    tool = state.tool(invocation.call.name)
    if tool is None:  # unreachable: REQUIRE_APPROVAL requires an enabled tool
        raise InvalidEventSequence("approval for a tool not enabled in this run")
    expected = approval_binding(state.run_id, invocation.invocation_id, tool, invocation.arguments)
    if event.binding != expected:
        raise InvalidEventSequence("approval binding does not match the pending invocation")
    resolved = invocation.model_copy(
        update={
            "approved": event.approved,
            "approver": event.approver,
            "approval_reason": event.reason,
        }
    )
    return _replace(state, resolved)


def _tool_execution_started(state: RunState, event: ToolExecutionStarted) -> dict[str, Any]:
    invocation = _invocation(state, event.invocation_id)
    turn = state.turns[-1].invocations
    if any(i.decision is None for i in turn):
        raise InvalidEventSequence("execution before every tool call in the turn is decided")
    if any(i.awaiting_approval for i in turn):
        raise InvalidEventSequence("execution while an approval in the turn is pending")
    if not invocation.authorized:
        raise InvalidEventSequence(f"{event.invocation_id!r} is not authorized")
    if invocation.finished:
        raise InvalidEventSequence(f"{event.invocation_id!r} already has a result")
    if event.attempt != invocation.attempts + 1:
        raise InvalidEventSequence(f"attempt {event.attempt} after {invocation.attempts}")
    tool = state.tool(invocation.call.name)
    if event.attempt > 1 and (tool is None or not tool.idempotent):
        raise InvalidEventSequence("re-execution requires an idempotent tool")
    return _replace(state, invocation.model_copy(update={"attempts": event.attempt}))


_EXECUTED = frozenset(
    {ToolOutcome.SUCCESS, ToolOutcome.ERROR, ToolOutcome.TIMEOUT, ToolOutcome.AMBIGUOUS}
)


def _tool_result_recorded(state: RunState, event: ToolResultRecorded) -> dict[str, Any]:
    invocation = _invocation(state, event.invocation_id)
    if invocation.finished:
        raise InvalidEventSequence(f"{event.invocation_id!r} already has a result")
    if invocation.decision is None:
        raise InvalidEventSequence("result for an undecided tool call")
    outcome = event.outcome
    if outcome in _EXECUTED and invocation.attempts == 0:
        raise InvalidEventSequence(f"{outcome} result for a tool call that never started")
    if outcome is ToolOutcome.INVALID and invocation.decision is not Decision.INVALID:
        raise InvalidEventSequence("invalid result for a call that was not rejected as invalid")
    if outcome is ToolOutcome.DENIED and not (
        invocation.decision is Decision.DENY or invocation.approved is False
    ):
        raise InvalidEventSequence("denied result without a denial or refused approval")
    if outcome in (ToolOutcome.INVALID, ToolOutcome.DENIED) and invocation.attempts:
        raise InvalidEventSequence("a started call cannot be recorded as invalid or denied")
    finished = invocation.model_copy(
        update={
            "outcome": outcome,
            "parts": event.parts,
            "truncated": event.truncated,
            "original_chars": event.original_chars,
            "artifact": event.artifact,
        }
    )
    return _replace(state, finished)


# --- context ------------------------------------------------------------------------------------


def _previous_op(state: RunState) -> str | None:
    return operation_id_for(state.run_id, len(state.turns) - 1) if state.turns else None


def _memory_recalled(state: RunState, event: MemoryRecalled) -> dict[str, Any]:
    if state.memory_recalled or state.turns or state.context is not None:
        raise InvalidEventSequence("memories are recalled once, before the first model call")
    granted = state.memory_access.read if state.memory_access is not None else ()
    if not set(event.scopes) <= set(granted):
        raise InvalidEventSequence("recall from memory scopes not granted to the run")
    for memory in event.memories:
        if memory.scope not in event.scopes:
            raise InvalidEventSequence(
                f"memory {memory.memory_id!r} is outside the recalled scopes"
            )
    if len({m.memory_id for m in event.memories}) != len(event.memories):
        raise InvalidEventSequence("duplicate recalled memory")
    return {"memory_recalled": True, "memories": event.memories}


def _context_compacted(state: RunState, event: ContextCompacted) -> dict[str, Any]:
    if state.pending_invocations:
        raise InvalidEventSequence("compaction while tool calls are unanswered")
    if event.compaction != len(state.compactions):
        raise InvalidEventSequence(f"compaction {event.compaction} after {len(state.compactions)}")
    if len(state.compactions) >= state.context_policy.max_compactions:
        raise InvalidEventSequence("max_compactions exceeded")
    through = event.summary.through_turn
    if not state.compacted_through < through <= len(state.turns) - 1:
        raise InvalidEventSequence(
            f"compaction through turn {through} must cover new turns and keep the latest one"
        )
    ids = [item.item_id for item in event.summary.items]
    if len(set(ids)) != len(ids):
        raise InvalidEventSequence("duplicate summary item ids")
    previous: set[str] = (
        {item.item_id for item in state.compactions[-1].items} if state.compactions else set()
    )
    resolved = {r.item_id for r in event.summary.resolved}
    if not resolved <= previous:
        raise InvalidEventSequence("summary resolves items that were not active")
    dropped = previous - set(ids) - resolved
    if dropped:
        raise InvalidEventSequence(f"summary drops items without resolving them: {sorted(dropped)}")
    return {"compactions": (*state.compactions, event.summary)}


def _context_assembled(state: RunState, event: ContextAssembled) -> dict[str, Any]:
    if state.pending_invocations:
        raise InvalidEventSequence("context assembled while tool calls are unanswered")
    expected = operation_id_for(state.run_id, len(state.turns))
    if event.operation_id != expected:
        raise InvalidEventSequence(f"context for {event.operation_id!r}, expected {expected!r}")
    if state.context is not None and state.context.operation_id == expected:
        raise InvalidEventSequence("context already assembled for this model call")
    if event.history_start != state.compacted_through:
        raise InvalidEventSequence(
            f"history must start at turn {state.compacted_through}, where the latest summary "
            f"ends (got {event.history_start})"
        )
    clearable = {
        i.invocation_id
        for turn in state.turns[event.history_start : len(state.turns) - 1]
        for i in turn.invocations
        if i.artifact is not None
    }
    if not set(event.cleared) <= clearable:
        raise InvalidEventSequence("only artifact-backed results of earlier turns can be cleared")
    recalled = {m.memory_id for m in state.memories}
    if not set(event.memories) <= recalled:
        raise InvalidEventSequence("context includes memories that were not recalled")
    if not set(event.passages) <= {p.key for p in state.passages}:
        raise InvalidEventSequence("context includes passages that were not retrieved")
    return {"context": event}


def _knowledge_retrieved(state: RunState, event: KnowledgeRetrieved) -> dict[str, Any]:
    if state.knowledge_retrieved or state.turns or state.context is not None:
        raise InvalidEventSequence("knowledge is retrieved once, before the first model call")
    access = state.knowledge_access
    if access is None or not set(event.collections) <= set(access.collections):
        raise InvalidEventSequence("retrieval from collections not granted to the run")
    for index, passage in enumerate(event.passages, start=1):
        if passage.key != f"S{index}":
            raise InvalidEventSequence("passage keys must be S1..Sn in order")
        if passage.collection not in event.collections or not access.can_see(
            passage.collection, passage.labels
        ):
            raise InvalidEventSequence(f"passage {passage.key} is not visible to the run's grant")
    if len({p.chunk_id for p in event.passages}) != len(event.passages):
        raise InvalidEventSequence("duplicate retrieved passage")
    return {"knowledge_retrieved": True, "passages": event.passages}
