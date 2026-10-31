"""Context assembly: planning what a model call contains, and building it from the plan.

`plan` decides, from run state alone, which turns are sent verbatim, which old tool results are
replaced by artifact references, which recalled memories are included, and whether earlier turns
must be compacted first. `build_request` turns a recorded plan (`ContextAssembled`) back into the
exact `ModelRequest`, so every attempt of a logical call sends the same request.

Levers, applied only when the estimate exceeds `compact_at` of the budget, cheapest and least
lossy first:
1. replace artifact-backed tool results of older turns with references (retrievable on demand);
2. compact older turns into a summary (ledger rebuilt from events, notes carried forward);
3. drop recalled memories, lowest ranked first, if the request still does not fit;
4. otherwise report an overflow: the mandatory context does not fit and the run fails rather
   than silently discarding it.
Mandatory context: instructions, input, tool specs, the latest turn and the latest summary.
"""

import json
import math
from dataclasses import dataclass, field

from hollis.runtime.context import (
    CompactionSummary,
    ContextBudget,
    TokenAccounting,
    TokenEstimate,
    calibrated,
    context_budget,
    estimate_parts,
)
from hollis.runtime.conversation import (
    AssistantTurn,
    ContextBlock,
    ContextKind,
    HistoryItem,
    ToolResultMessage,
    ToolResultsTurn,
    ToolSpec,
    render_context,
)
from hollis.runtime.events import ContextAssembled, ToolOutcome
from hollis.runtime.model import ModelCapabilities, ModelRequest
from hollis.runtime.results import render_parts, to_message
from hollis.runtime.state import Invocation, RunState
from hollis.runtime.tools import ToolEffect

LEDGER_PREVIEW_CHARS = 240
TRANSCRIPT_RESULT_CHARS = 1500
TRANSCRIPT_MAX_CHARS = 120_000


@dataclass(frozen=True)
class ContextPlan:
    operation_id: str
    history_start: int
    cleared: tuple[str, ...]
    memories: tuple[str, ...]
    estimate: TokenEstimate
    budget: ContextBudget
    excluded: tuple[str, ...] = ()
    compact_through: int | None = None
    """When set, compact turns before this index first, then plan again."""
    overflow: bool = False
    notes: tuple[str, ...] = field(default=())

    def record(self) -> ContextAssembled:
        return ContextAssembled(
            operation_id=self.operation_id,
            history_start=self.history_start,
            cleared=self.cleared,
            memories=self.memories,
            estimate=self.estimate,
            budget=self.budget,
            excluded=self.excluded,
        )


def calibration(state: RunState) -> float:
    """Ratio of provider-reported to heuristically estimated input tokens for the latest turn
    that has both. Used only to scale estimates up (see context.calibrated)."""
    for turn in reversed(state.turns):
        usage = turn.response.usage
        if turn.estimate is not None and usage is not None and turn.estimate.heuristic_tokens > 0:
            return max(1.0, usage.total_input_tokens / turn.estimate.heuristic_tokens)
    return 1.0


def plan(
    state: RunState,
    operation_id: str,
    tools: tuple[ToolSpec, ...],
    capabilities: ModelCapabilities,
    *,
    factor: float = 1.0,
    can_compact: bool = True,
) -> ContextPlan:
    policy = state.context_policy
    window = capabilities.text.context_window_tokens if capabilities.text else None
    budget = context_budget(policy, window, state.budget.max_output_tokens)
    limit = budget.input_tokens
    start = state.compacted_through
    turns = len(state.turns)
    memories = [m.memory_id for m in state.memories]
    cleared: list[str] = []
    excluded: list[str] = []
    compact_through: int | None = None

    def estimate() -> TokenEstimate:
        request = _request(state, operation_id, start, set(cleared), tuple(memories), tools)
        heuristic = estimate_parts(
            instructions=request.instructions,
            context=render_context(request.context) if request.context else None,
            input_text=request.input,
            history=request.history,
            tools=request.tools,
        )
        return calibrated(heuristic, factor)

    current = estimate()
    compactions_left = can_compact and len(state.compactions) < policy.max_compactions
    keep = policy.keep_recent_turns
    if (
        compactions_left
        and policy.max_history_turns is not None
        and turns - start > policy.max_history_turns
        and turns - keep > start
    ):
        compact_through = turns - keep

    if limit is not None and compact_through is None:
        threshold = math.floor(limit * policy.compact_at)
        if current.input_tokens > threshold:
            # 1. Artifact-backed results of older turns become references, oldest first.
            for invocation in _clearable(state, start, turns - keep):
                cleared.append(invocation.invocation_id)
                current = estimate()
                if current.input_tokens <= threshold:
                    break
        if current.input_tokens > threshold and compactions_left:
            # 2. Compact what is not kept verbatim; if that is nothing, keep only the latest turn.
            cut = turns - keep if turns - keep > start else turns - 1
            if cut > start:
                compact_through = cut
        if compact_through is None and current.input_tokens > limit:
            # Still too large and nothing left to compact: clear every eligible older result.
            for invocation in _clearable(state, start, turns - 1):
                if invocation.invocation_id not in cleared:
                    cleared.append(invocation.invocation_id)
                    current = estimate()
                    if current.input_tokens <= limit:
                        break
        if compact_through is None:
            # 3. Recalled memories are optional: drop the lowest ranked until it fits.
            while current.input_tokens > limit and memories:
                dropped = memories.pop()
                excluded.append(f"memory {dropped}: dropped to fit the input budget")
                current = estimate()

    if cleared:
        excluded.append(f"{len(cleared)} older tool result(s) replaced by artifact references")
    overflow = compact_through is None and limit is not None and current.input_tokens > limit
    notes: list[str] = []
    if limit is None:
        notes.append("input budget unknown: no context window declared and no max_input_tokens")
    return ContextPlan(
        operation_id=operation_id,
        history_start=start,
        cleared=tuple(cleared),
        memories=tuple(memories),
        estimate=current,
        budget=budget,
        excluded=tuple(excluded),
        compact_through=compact_through,
        overflow=overflow,
        notes=tuple(notes),
    )


def with_count(plan_: ContextPlan, counted: int) -> ContextPlan:
    """The same plan with the provider's exact count as its estimate."""
    estimate = TokenEstimate(
        input_tokens=counted,
        accounting=TokenAccounting.COUNTED,
        heuristic_tokens=plan_.estimate.heuristic_tokens,
        calibration=plan_.estimate.calibration,
    )
    limit = plan_.budget.input_tokens
    return ContextPlan(
        operation_id=plan_.operation_id,
        history_start=plan_.history_start,
        cleared=plan_.cleared,
        memories=plan_.memories,
        estimate=estimate,
        budget=plan_.budget,
        excluded=plan_.excluded,
        overflow=limit is not None and counted > limit,
        notes=plan_.notes,
    )


def build_request(
    state: RunState,
    operation_id: str,
    tools: tuple[ToolSpec, ...],
    timeout_s: float,
    context: ContextAssembled | None,
) -> ModelRequest:
    """The request for a logical model call. Without a recorded plan (runs from before M4), the
    whole history is sent, as M3 did."""
    if context is None:
        request = _request(state, operation_id, 0, set(), (), tools, summary=False)
    else:
        request = _request(
            state,
            operation_id,
            context.history_start,
            set(context.cleared),
            context.memories,
            tools,
        )
    return request.model_copy(update={"timeout_s": timeout_s})


def _request(
    state: RunState,
    operation_id: str,
    start: int,
    cleared: set[str],
    memories: tuple[str, ...],
    tools: tuple[ToolSpec, ...],
    *,
    summary: bool = True,
) -> ModelRequest:
    blocks: list[ContextBlock] = []
    included = set(memories)
    blocks.extend(
        ContextBlock(kind=ContextKind.MEMORY, source=f"memory:{m.memory_id}", text=m.render())
        for m in state.memories
        if m.memory_id in included
    )
    if summary and state.compactions and start > 0:
        blocks.append(summary_block(state, state.compactions[-1], len(state.compactions) - 1))
    return ModelRequest(
        operation_id=operation_id,
        model=state.deployment.model,
        input=state.input,
        instructions=state.instructions,
        context=tuple(blocks),
        history=history(state, start, cleared),
        tools=tools,
        max_output_tokens=state.budget.max_output_tokens,
        timeout_s=state.budget.attempt_timeout_s,
    )


def history(
    state: RunState, start: int = 0, cleared: set[str] | None = None
) -> tuple[HistoryItem, ...]:
    cleared = cleared or set()
    items: list[HistoryItem] = []
    for turn in state.turns[start:]:
        response = turn.response
        items.append(
            AssistantTurn(
                content=response.content,
                tool_calls=response.tool_calls,
                continuation=response.continuation,
            )
        )
        if turn.invocations:
            items.append(
                ToolResultsTurn(
                    results=tuple(
                        _result(state, i, i.invocation_id in cleared) for i in turn.invocations
                    )
                )
            )
    return tuple(items)


def _result(state: RunState, invocation: Invocation, cleared: bool) -> ToolResultMessage:
    name = invocation.call.name
    outcome = invocation.outcome or ToolOutcome.ERROR
    if cleared and invocation.artifact is not None:
        ref = invocation.artifact
        text = (
            f"[result cleared from context to save space: {name} -> {outcome.value}, "
            f"{ref.chars} characters, stored as artifact {ref.artifact_id}]"
        )
        return ToolResultMessage(
            call_id=invocation.call.call_id,
            name=name,
            content=text,
            is_error=outcome is not ToolOutcome.SUCCESS,
        )
    return to_message(invocation.call.call_id, name, outcome, invocation.parts, state.tool(name))


def _clearable(state: RunState, start: int, end: int) -> list[Invocation]:
    return [
        i
        for turn in state.turns[start : max(end, start)]
        for i in turn.invocations
        if i.artifact is not None
    ]


# --- summaries --------------------------------------------------------------------------------


def summary_block(state: RunState, summary: CompactionSummary, index: int) -> ContextBlock:
    return ContextBlock(
        kind=ContextKind.SUMMARY,
        source=f"summary:{index}",
        text=render_summary(state, summary, index),
    )


def render_summary(state: RunState, summary: CompactionSummary, index: int) -> str:
    through = summary.through_turn
    lines = [
        f"Summary of turns 0-{through - 1} of this run (compaction {index}, "
        f"method {summary.method.value}). The complete record is kept in the run log.",
        "",
        "Actions performed (rebuilt from the run log, not summarized):",
    ]
    lines.extend(ledger(state, through) or ["- none"])
    if summary.state:
        lines += ["", "Current state:", summary.state]
    for title, prefix in (
        ("Decisions", "decision-"),
        ("Observations", "observation-"),
        ("Outstanding work and open questions", "open-"),
    ):
        items = [i for i in summary.items if i.item_id.startswith(prefix)]
        if items:
            lines += ["", f"{title}:"]
            lines.extend(
                f"- [{i.item_id}] {i.text}"
                + (f" (sources: {', '.join(i.sources)})" if i.sources else "")
                for i in items
            )
    return "\n".join(lines)


def ledger(state: RunState, through: int) -> list[str]:
    """Every action in turns [0, through): side effects, failures and unknown outcomes listed
    individually; successful read-only calls aggregated per tool."""
    lines: list[str] = []
    reads: dict[str, list[str]] = {}
    for turn_index, turn in enumerate(state.turns[:through]):
        for i in turn.invocations:
            ref = state.tool(i.call.name)
            outcome = i.outcome or ToolOutcome.ERROR
            read_only = ref is not None and ref.effect is ToolEffect.READ_ONLY
            preview = _preview(render_parts(i.parts))
            artifact = f" [artifact {i.artifact.artifact_id}]" if i.artifact else ""
            if read_only and outcome is ToolOutcome.SUCCESS:
                reads.setdefault(i.call.name, []).append(f"turn {turn_index}{artifact}")
                continue
            lines.append(
                f"- turn {turn_index} call {i.index}: "
                f"{i.call.name}({_arguments(i)}) -> {outcome.value}: {preview}{artifact}"
            )
    for name, uses in reads.items():
        lines.append(f"- read-only {name}: {len(uses)} successful call(s) ({', '.join(uses[-5:])})")
    return lines


def _arguments(invocation: Invocation) -> str:
    arguments = (
        invocation.arguments if invocation.arguments is not None else invocation.call.arguments
    )
    return _preview(json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False), 160)


def _preview(text: str, limit: int = LEDGER_PREVIEW_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def transcript(state: RunState, start: int, through: int) -> str:
    """Turns [start, through) as text for a summarizer: assistant messages, tool calls and
    result previews (results are capped; the ledger keeps every action regardless)."""
    parts: list[str] = []
    for turn_index in range(start, through):
        turn = state.turns[turn_index]
        if turn.response.content:
            parts.append(f"[turn {turn_index} assistant]\n{turn.response.content}")
        for i in turn.invocations:
            outcome = (i.outcome or ToolOutcome.ERROR).value
            result = render_parts(i.parts)
            if len(result) > TRANSCRIPT_RESULT_CHARS:
                result = result[:TRANSCRIPT_RESULT_CHARS] + " …[shortened]"
            parts.append(
                f"[turn {turn_index} tool {i.call.name} {_arguments(i)} -> {outcome}]\n{result}"
            )
    text = "\n\n".join(parts)
    if len(text) > TRANSCRIPT_MAX_CHARS:
        # Keep the most recent material; the ledger still lists every earlier action.
        text = "…[earlier material shortened]\n" + text[-TRANSCRIPT_MAX_CHARS:]
    return text
