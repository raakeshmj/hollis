"""Run views computed from the runtime's own state and events (ADR-0027).

The inspector shows what the runtime recorded: turns, model attempts, context plans, tool
decisions, approvals, results, evidence and failures. Everything here reads the folded `RunState`
and the event log; nothing re-implements run semantics.
"""

from datetime import datetime, timedelta
from typing import Any

from hollis.platform.schemas import (
    ApprovalRequestView,
    AttemptView,
    CancellationView,
    CompactionView,
    ContextView,
    EventView,
    FailureView,
    ModelCallView,
    RunDetail,
    RunPhase,
    RunSummary,
    ToolCallView,
    TurnView,
    UsageView,
)
from hollis.runtime.events import (
    AnswerGrounded,
    ContextAssembled,
    ContextCompacted,
    EventEnvelope,
    KnowledgeRetrieved,
    MemoryRecalled,
    ModelCallFailed,
    ModelCallStarted,
    ModelResponseRecorded,
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
from hollis.runtime.results import render_parts
from hollis.runtime.state import Invocation, RunState, RunStatus, operation_id_for
from hollis.runtime.tools import Decision, approval_binding

RESULT_PREVIEW_CHARS = 4000
TERMINAL_STATUSES = frozenset(s.value for s in RunStatus if s.is_terminal)
INPUT_PREVIEW_CHARS = 160

_OUTCOME_STATUS = {
    ToolOutcome.SUCCESS: "succeeded",
    ToolOutcome.ERROR: "failed",
    ToolOutcome.INVALID: "invalid",
    ToolOutcome.DENIED: "denied",
    ToolOutcome.TIMEOUT: "timed_out",
    ToolOutcome.AMBIGUOUS: "ambiguous",
}


def run_phase(
    *, status: str, awaiting: bool, active: bool, started: bool, stopped: bool
) -> RunPhase:
    """The phase interfaces show, from the run's status and whether anything drives it now. One
    definition, used for run lists (from the index) and run details (from the fold)."""
    if status in TERMINAL_STATUSES:
        return RunPhase(status)
    if awaiting:
        return RunPhase.WAITING_APPROVAL
    if active:
        return RunPhase.RUNNING
    return RunPhase.INTERRUPTED if started or stopped else RunPhase.QUEUED


def phase(state: RunState, *, active: bool, stopped: bool = False) -> RunPhase:
    return run_phase(
        status=state.status.value,
        awaiting=bool(state.awaiting_approval),
        active=active,
        started=state.status is not RunStatus.PENDING or bool(state.turns),
        stopped=stopped,
    )


def clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def summary(
    state: RunState,
    *,
    active: bool,
    updated_at: datetime,
    agent: str | None = None,
    created_by: str | None = None,
    session: str | None = None,
    error: str | None = None,
) -> RunSummary:
    calls = sum(len(t.invocations) for t in state.turns)
    failure = state.failure.message if state.failure else None
    return RunSummary(
        run_id=state.run_id,
        phase=phase(state, active=active, stopped=error is not None),
        status=state.status.value,
        agent=agent,
        created_by=created_by,
        session=session,
        model=state.model,
        provider=state.deployment.provider,
        protocol=state.deployment.protocol,
        input_preview=clip(state.input, INPUT_PREVIEW_CHARS),
        created_at=state.created_at,
        updated_at=updated_at,
        awaiting_approvals=len(state.awaiting_approval),
        turns=len(state.turns),
        tool_calls=calls,
        usage=UsageView(
            input_tokens=state.usage.input_tokens, output_tokens=state.usage.output_tokens
        ),
        error=failure or (error if not state.status.is_terminal else None),
        duration_s=max((updated_at - state.created_at).total_seconds(), 0.0),
    )


def _tool_call(
    state: RunState, turn: int, invocation: Invocation, times: dict[str, dict[str, datetime]]
) -> ToolCallView:
    ref = state.tool(invocation.call.name)
    if invocation.decision is None:
        status = "deciding"
    elif invocation.awaiting_approval:
        status = "awaiting_approval"
    elif invocation.outcome is not None:
        status = _OUTCOME_STATUS[invocation.outcome]
    elif invocation.authorized:
        status = "running" if invocation.attempts > 0 else "queued"
    else:
        status = "denied" if invocation.decision is Decision.DENY else "invalid"
    approval = None
    if invocation.awaiting_approval and ref is not None and invocation.decided_at is not None:
        approval = ApprovalRequestView(
            binding=approval_binding(
                state.run_id, invocation.invocation_id, ref, invocation.arguments
            ),
            expires_at=invocation.decided_at + timedelta(seconds=state.budget.approval_timeout_s),
            reason=invocation.reason or "",
        )
    result = render_parts(invocation.parts) if invocation.outcome is not None else None
    stamps = times.get(invocation.invocation_id, {})
    started, finished = stamps.get("started"), stamps.get("finished")
    return ToolCallView(
        invocation_id=invocation.invocation_id,
        turn=turn,
        index=invocation.index,
        tool=invocation.call.name,
        arguments=invocation.arguments
        if invocation.arguments is not None
        else invocation.call.arguments,
        raw_arguments=invocation.call.raw_arguments,
        status=status,  # type: ignore[arg-type]  # one of the literal statuses above
        decision=invocation.decision.value if invocation.decision else None,
        reason=invocation.reason,
        rule=invocation.rule,
        approved=invocation.approved,
        approver=invocation.approver,
        approval_reason=invocation.approval_reason,
        approval=approval,
        effect=ref.effect.value if ref else None,
        trust=ref.trust.value if ref else None,
        attempts=invocation.attempts,
        outcome=invocation.outcome.value if invocation.outcome else None,
        result=result[:RESULT_PREVIEW_CHARS] if result is not None else None,
        result_truncated=invocation.truncated
        or (result is not None and len(result) > RESULT_PREVIEW_CHARS),
        result_chars=invocation.original_chars,
        artifact_id=invocation.artifact.artifact_id if invocation.artifact else None,
        duration_ms=int((finished - started).total_seconds() * 1000)
        if started and finished
        else None,
        started_at=started,
        finished_at=finished,
    )


def detail(
    state: RunState,
    events: list[EventEnvelope],
    *,
    active: bool,
    agent: str | None = None,
    created_by: str | None = None,
    session: str | None = None,
    error: str | None = None,
) -> RunDetail:
    attempts: dict[str, list[dict[str, Any]]] = {}
    responded: dict[str, datetime] = {}
    contexts: dict[str, ContextAssembled] = {}
    tool_times: dict[str, dict[str, datetime]] = {}
    for event in events:
        payload = event.payload
        match payload:
            case ModelCallStarted(operation_id=op, attempt=n):
                attempts.setdefault(op, []).append(
                    {"attempt": n, "started_at": event.occurred_at, "outcome": "in_flight"}
                )
            case ModelCallFailed(operation_id=op, attempt=n, failure=failure):
                for entry in attempts.get(op, []):
                    if entry["attempt"] == n:
                        entry |= {
                            "outcome": "failed",
                            "finished_at": event.occurred_at,
                            "failure_kind": failure.kind.value,
                            "message": failure.message,
                        }
            case ModelResponseRecorded(operation_id=op, attempt=n):
                responded[op] = event.occurred_at
                for entry in attempts.get(op, []):
                    if entry["attempt"] == n:
                        entry |= {"outcome": "responded", "finished_at": event.occurred_at}
            case ContextAssembled(operation_id=op):
                contexts[op] = payload
            case ToolExecutionStarted(invocation_id=inv):
                tool_times.setdefault(inv, {})["started"] = event.occurred_at
            case ToolResultRecorded(invocation_id=inv):
                tool_times.setdefault(inv, {})["finished"] = event.occurred_at
            case _:
                pass

    def model_call(op: str, turn_index: int | None) -> ModelCallView:
        context = contexts.get(op)
        response = state.turns[turn_index].response if turn_index is not None else None
        tries = attempts.get(op, [])
        first = tries[0]["started_at"] if tries else None
        done = responded.get(op)
        return ModelCallView(
            operation_id=op,
            attempts=tuple(AttemptView.model_validate(t) for t in tries),
            text=response.content if response else None,
            stop_reason=response.stop_reason.value if response else None,
            usage=UsageView(
                input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens
            )
            if response and response.usage
            else None,
            served_model=response.served_model if response else None,
            context=ContextView(
                estimate=context.estimate.input_tokens,
                accounting=context.estimate.accounting,
                budget=context.budget.input_tokens,
                window=context.budget.window_tokens,
                history_start=context.history_start,
                cleared=len(context.cleared),
                memories=len(context.memories),
                passages=len(context.passages),
                excluded=context.excluded,
            )
            if context
            else None,
            duration_ms=int((done - first).total_seconds() * 1000) if first and done else None,
        )

    turns = [
        TurnView(
            index=i,
            model_call=model_call(operation_id_for(state.run_id, i), i),
            tool_calls=tuple(_tool_call(state, i, inv, tool_times) for inv in turn.invocations),
        )
        for i, turn in enumerate(state.turns)
    ]
    in_flight = operation_id_for(state.run_id, len(state.turns))
    if in_flight in attempts:
        turns.append(TurnView(index=len(state.turns), model_call=model_call(in_flight, None)))
    final = state.response
    updated = events[-1].occurred_at if events else state.created_at
    return RunDetail(
        summary=summary(
            state,
            active=active,
            updated_at=updated,
            agent=agent,
            created_by=created_by,
            session=session,
            error=error,
        ),
        input=state.input,
        instructions=state.instructions,
        budget=state.budget,
        context_policy=state.context_policy,
        tools=tuple(t.name for t in state.tools),
        memory_access=state.memory_access,
        knowledge_access=state.knowledge_access,
        memories=state.memories,
        passages=state.passages,
        grounding=state.grounding,
        compactions=tuple(
            CompactionView(
                index=i,
                through_turn=c.through_turn,
                method=c.method.value,
                items=len(c.items),
                summary=c,
            )
            for i, c in enumerate(state.compactions)
        ),
        turns=tuple(turns),
        answer=final.content if state.status is RunStatus.COMPLETED and final is not None else None,
        failure=FailureView(
            reason=state.failure.reason.value,
            message=state.failure.message,
            kind=state.failure.kind.value if state.failure.kind else None,
        )
        if state.failure
        else None,
        cancellation=CancellationView(
            by=state.cancellation.by,
            reason=state.cancellation.reason,
            interrupted=state.cancellation.interrupted,
        )
        if state.cancellation
        else None,
        deadline=state.deadline,
        assignment=state.assignment,
        delegation=state.delegation,
    )


def event_views(events: list[EventEnvelope]) -> list[EventView]:
    tools: dict[str, str] = {}
    views: list[EventView] = []
    for event in events:
        payload = event.payload
        match payload:
            case RunCreated(model=model, tools=refs):
                text = f"Run created on {model}" + (f" with {len(refs)} tools" if refs else "")
            case MemoryRecalled(memories=memories):
                text = f"Recalled {len(memories)} memories"
            case KnowledgeRetrieved(passages=passages):
                text = f"Retrieved {len(passages)} knowledge passages"
            case ContextAssembled(estimate=estimate, budget=budget):
                limit = budget.input_tokens
                accounting = estimate.accounting.value
                text = f"Context assembled: ~{estimate.input_tokens} tokens ({accounting})" + (
                    f" of {limit}" if limit is not None else ", budget unknown"
                )
            case ContextCompacted(summary=compacted):
                text = f"Compacted turns 0-{compacted.through_turn - 1} ({compacted.method.value})"
            case ModelCallStarted(operation_id=op, attempt=n):
                text = f"Model call {op.rsplit('/', 1)[-1]} started (attempt {n})"
            case ModelResponseRecorded(response=response):
                calls = len(response.tool_calls)
                text = f"Model responded ({response.stop_reason.value})" + (
                    f", {calls} tool call(s)" if calls else ""
                )
            case ModelCallFailed(failure=failure):
                text = f"Model call failed: {failure.kind.value}"
            case ToolCallDecided(invocation_id=inv, tool=tool, decision=decision, reason=reason):
                tools[inv] = tool
                text = f"{tool}: {decision.value.replace('_', ' ')}" + (
                    f" ({clip(reason, 80)})" if reason else ""
                )
            case ToolApprovalResolved(invocation_id=inv, approved=approved, approver=approver):
                verdict = "approved" if approved else "denied"
                text = f"{tools.get(inv, 'tool')}: {verdict} by {approver}"
            case ToolExecutionStarted(invocation_id=inv, attempt=n):
                text = f"{tools.get(inv, 'tool')} started" + (f" (attempt {n})" if n > 1 else "")
            case ToolResultRecorded(invocation_id=inv, outcome=outcome):
                text = f"{tools.get(inv, 'tool')} {outcome.value}"
            case AnswerGrounded(report=report):
                supported = sum(1 for c in report.claims if c.support.value == "supported")
                text = f"Grounding: {supported}/{len(report.claims)} claims supported" + (
                    f", {len(report.fabricated)} fabricated citation(s)"
                    if report.fabricated
                    else ""
                )
            case RunCompleted():
                text = "Run completed"
            case RunFailed(reason=reason, message=message):
                text = f"Run failed ({reason.value}): {clip(message, 120)}"
            case RunCancelled(by=by):
                text = f"Cancelled by {by}"
            case _:
                text = event.event_type.value.replace("_", " ")
        views.append(
            EventView(
                sequence=event.sequence,
                type=event.event_type.value,
                occurred_at=event.occurred_at,
                summary=text,
                payload=payload.model_dump(mode="json"),
            )
        )
    return views
