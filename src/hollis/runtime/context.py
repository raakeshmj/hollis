"""Context engine types: policy, token budgets, estimates and compaction summaries (ADR-0018).

This module holds the provider-independent vocabulary of context management. Planning (which
turns, results and memories go into a request) lives in `hollis.runtime.assembly`, which reads
run state; compaction summarizers live in `hollis.runtime.compaction`.

Token accounting is explicit about what it knows:
- `estimated`: a conservative character heuristic (no tokenizer is exact for every model);
- `calibrated`: the heuristic scaled up by the ratio of provider-reported to estimated input
  tokens observed earlier in the same run (never scaled down);
- `counted`: the provider's own tokenizer, through the optional `TokenCounter` port.
Actual usage is what the provider reports after a call (`Usage`); it is never confused with an
estimate. A deployment without a declared context window has an unknown window, and the record
says so.
"""

import json
import math
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from hollis.runtime.conversation import (
    AssistantTurn,
    HistoryItem,
    ToolSpec,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ContextPolicy(_Frozen):
    """How a run's context is budgeted and maintained. Persisted in RunCreated."""

    max_input_tokens: int | None = Field(default=None, ge=256)
    """Configured input budget. The effective budget is the smaller of this and what the
    deployment's context window leaves after the output reservation and safety margin."""
    safety_margin: float = Field(default=0.1, ge=0, le=0.5)
    """Fraction of the context window kept free to absorb estimation error."""
    compact_at: float = Field(default=0.8, gt=0, le=1)
    """Apply context levers once the estimate exceeds this fraction of the budget. Below it,
    history is left untouched so the provider's prompt cache prefix stays stable."""
    keep_recent_turns: int = Field(default=2, ge=1, le=50)
    """Latest turns always kept verbatim by compaction (the latest one always is)."""
    max_history_turns: int | None = Field(default=None, ge=1, le=1000)
    """Also compact when more turns than this are uncompacted, whatever their size."""
    max_compactions: int = Field(default=50, ge=0, le=1000)
    memory_limit: int = Field(default=8, ge=0, le=100)
    """Memories recalled into context at run start."""
    memory_chars: int = Field(default=6000, ge=0, le=200_000)
    """Rendered size limit for memories recalled at run start."""
    summary_max_output_tokens: int = Field(default=2048, ge=128, le=32_000)
    """Output limit for a model-assisted compaction call."""
    verify_with_provider_at: float | None = Field(default=0.9, gt=0, le=1)
    """Ask the provider for an exact count (if it can) once the estimate exceeds this fraction
    of the budget. None disables exact counting."""
    artifact_threshold_chars: int = Field(default=4000, ge=256, le=1_000_000)
    """Tool results longer than this are stored whole as artifacts (when the runner has an
    artifact store); the run log and context get a preview and a reference."""
    artifact_preview_chars: int = Field(default=2000, ge=0, le=100_000)
    knowledge_limit: int = Field(default=6, ge=0, le=50)
    """Passages retrieved into context at run start (0 disables; tools still work)."""
    knowledge_chars: int = Field(default=8000, ge=0, le=200_000)
    knowledge_include_flagged: bool = False
    """Include chunks flagged as instruction-like in automatic context (tools always show them,
    with a warning)."""
    check_grounding: bool = True
    """Record a grounding report for final answers when the run had knowledge evidence."""


class TokenAccounting(StrEnum):
    ESTIMATED = "estimated"
    CALIBRATED = "calibrated"
    COUNTED = "counted"


class TokenEstimate(_Frozen):
    input_tokens: int = Field(ge=0)
    """The number used for budgeting."""
    accounting: TokenAccounting
    heuristic_tokens: int = Field(ge=0)
    """The uncalibrated heuristic, kept so later turns can calibrate against actual usage."""
    calibration: float = Field(default=1.0, ge=1.0)


class ContextBudget(_Frozen):
    window_tokens: int | None
    """The deployment's declared context window; None when not declared."""
    reserved_output_tokens: int = Field(ge=0)
    margin_tokens: int = Field(ge=0)
    max_input_tokens: int | None
    """The configured cap, if any."""

    @property
    def input_tokens(self) -> int | None:
        """Tokens available for input, or None if neither a window nor a cap is known."""
        available: int | None = None
        if self.window_tokens is not None:
            available = max(
                self.window_tokens - self.reserved_output_tokens - self.margin_tokens, 0
            )
        if self.max_input_tokens is not None:
            available = (
                self.max_input_tokens
                if available is None
                else min(available, self.max_input_tokens)
            )
        return available


def context_budget(
    policy: ContextPolicy, window_tokens: int | None, max_output_tokens: int
) -> ContextBudget:
    margin = math.ceil(window_tokens * policy.safety_margin) if window_tokens is not None else 0
    return ContextBudget(
        window_tokens=window_tokens,
        reserved_output_tokens=max_output_tokens,
        margin_tokens=margin,
        max_input_tokens=policy.max_input_tokens,
    )


# --- estimation -------------------------------------------------------------------------------

CHARS_PER_TOKEN = 3.0
"""ASCII characters per token. Current tokenizers average about four for English prose and fewer
for code, JSON and identifiers; three keeps the estimate on the high side."""
NON_ASCII_TOKENS_PER_CHAR = 1.0
"""CJK and most non-Latin scripts take roughly one token (or more) per character."""
MESSAGE_OVERHEAD_TOKENS = 8
MAX_CALIBRATION = 10.0


def estimate_text(text: str) -> int:
    if not text:
        return 0
    if text.isascii():
        return math.ceil(len(text) / CHARS_PER_TOKEN)
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    return math.ceil(
        ascii_chars / CHARS_PER_TOKEN + (len(text) - ascii_chars) * NON_ASCII_TOKENS_PER_CHAR
    )


def _json_tokens(value: object) -> int:
    return estimate_text(json.dumps(value, sort_keys=True, ensure_ascii=False))


def estimate_parts(
    *,
    instructions: str | None,
    context: str | None,
    input_text: str,
    history: tuple[HistoryItem, ...],
    tools: tuple[ToolSpec, ...],
) -> int:
    """Heuristic input tokens for a request's normalized content.

    Provider continuations (e.g. signed thinking blocks) are not counted: their size in tokens
    is provider-specific. Calibration against reported usage absorbs that overhead.
    """
    total = MESSAGE_OVERHEAD_TOKENS + estimate_text(input_text)
    if instructions:
        total += MESSAGE_OVERHEAD_TOKENS + estimate_text(instructions)
    if context:
        total += estimate_text(context)
    for item in history:
        total += MESSAGE_OVERHEAD_TOKENS
        if isinstance(item, AssistantTurn):
            total += estimate_text(item.content)
            for call in item.tool_calls:
                total += MESSAGE_OVERHEAD_TOKENS + estimate_text(call.name)
                total += (
                    estimate_text(call.raw_arguments)
                    if call.raw_arguments is not None
                    else _json_tokens(call.arguments or {})
                )
        else:
            for result in item.results:
                total += MESSAGE_OVERHEAD_TOKENS + estimate_text(result.content)
    for spec in tools:
        total += MESSAGE_OVERHEAD_TOKENS + _json_tokens(spec.model_dump(mode="json"))
    return total


def calibrated(heuristic: int, calibration: float) -> TokenEstimate:
    factor = min(max(calibration, 1.0), MAX_CALIBRATION)
    return TokenEstimate(
        input_tokens=math.ceil(heuristic * factor),
        accounting=TokenAccounting.CALIBRATED if factor > 1.0 else TokenAccounting.ESTIMATED,
        heuristic_tokens=heuristic,
        calibration=factor,
    )


# --- compaction summaries ---------------------------------------------------------------------

ITEM_ID_PATTERN = r"^(decision|observation|open)-[0-9]{1,6}$"
MAX_ITEM_CHARS = 1000
MAX_STATE_CHARS = 4000
MAX_NEW_ITEMS_PER_SECTION = 20


class SummarySection(StrEnum):
    DECISION = "decision"
    """A choice made during the task, with its rationale."""
    OBSERVATION = "observation"
    """Something learned that later work depends on."""
    OPEN = "open"
    """Outstanding work or an unresolved question."""


class SummaryItem(_Frozen):
    """One note in a compaction summary. Immutable once recorded: a later compaction may resolve
    it (with a reason) or add a new item, but never rewrites it."""

    item_id: str = Field(pattern=ITEM_ID_PATTERN)
    text: str = Field(min_length=1, max_length=MAX_ITEM_CHARS)
    sources: tuple[str, ...] = ()
    """Turn or invocation references the note is based on."""
    first_turn: int = Field(ge=0)
    """The latest turn covered when the item was added."""

    @property
    def section(self) -> SummarySection:
        return SummarySection(self.item_id.split("-", 1)[0])


class ResolvedItem(_Frozen):
    item_id: str = Field(pattern=ITEM_ID_PATTERN)
    reason: str = Field(min_length=1, max_length=MAX_ITEM_CHARS)


class CompactionMethod(StrEnum):
    MODEL = "model"
    DETERMINISTIC = "deterministic"
    DETERMINISTIC_FALLBACK = "deterministic_fallback"
    """A model summary was attempted and failed or was invalid; nothing from it was used."""


class CompactionSummary(_Frozen):
    """A derived representation of turns `[0, through_turn)`. The turns stay in the run log.

    The action ledger (every tool call, its outcome and artifact) is not stored here: it is
    rebuilt from the run's events whenever the summary is rendered, so it can never be eroded
    by repeated summarization.
    """

    through_turn: int = Field(ge=1)
    state: str = Field(default="", max_length=MAX_STATE_CHARS)
    """Where the task stands, as of `through_turn`."""
    items: tuple[SummaryItem, ...] = ()
    """Active notes: carried forward from earlier compactions unless resolved."""
    resolved: tuple[ResolvedItem, ...] = ()
    """Items resolved by this compaction, with reasons (they remain in earlier events)."""
    method: CompactionMethod
    fallback_reason: str | None = None


class DraftItem(_Frozen):
    section: SummarySection
    text: str = Field(min_length=1, max_length=MAX_ITEM_CHARS)
    sources: tuple[str, ...] = ()


class SummaryDraft(_Frozen):
    """What a summarizer proposes: new items, resolutions of existing items, and the state."""

    state: str = Field(default="", max_length=MAX_STATE_CHARS)
    new_items: tuple[DraftItem, ...] = ()
    resolved: tuple[ResolvedItem, ...] = ()


class SummaryRejected(ValueError):
    """A draft violates the summary contract; the deterministic fallback is used instead."""


def merge_summary(
    previous: CompactionSummary | None,
    draft: SummaryDraft,
    *,
    through_turn: int,
    method: CompactionMethod,
    fallback_reason: str | None = None,
) -> CompactionSummary:
    """Combine a draft with the previous summary under the carry-forward rule.

    Every previous item survives unless the draft resolves it by id with a reason. Resolving an
    unknown id, or adding too many items, rejects the draft (SummaryRejected).
    """
    active = list(previous.items) if previous is not None else []
    known = {item.item_id for item in active}
    resolved_ids = [r.item_id for r in draft.resolved]
    unknown = sorted(set(resolved_ids) - known)
    if unknown:
        raise SummaryRejected(f"resolves unknown items {unknown}")
    if len(set(resolved_ids)) != len(resolved_ids):
        raise SummaryRejected("resolves an item twice")
    for section in SummarySection:
        added = sum(1 for d in draft.new_items if d.section is section)
        if added > MAX_NEW_ITEMS_PER_SECTION:
            raise SummaryRejected(
                f"adds {added} {section.value} items (limit {MAX_NEW_ITEMS_PER_SECTION})"
            )
    kept = [item for item in active if item.item_id not in set(resolved_ids)]
    counters = {section: 0 for section in SummarySection}
    for item in active:
        counters[item.section] = max(counters[item.section], int(item.item_id.split("-")[1]))
    for draft_item in draft.new_items:
        counters[draft_item.section] += 1
        kept.append(
            SummaryItem(
                item_id=f"{draft_item.section.value}-{counters[draft_item.section]}",
                text=draft_item.text,
                sources=draft_item.sources,
                first_turn=through_turn - 1,
            )
        )
    return CompactionSummary(
        through_turn=through_turn,
        state=draft.state,
        items=tuple(kept),
        resolved=draft.resolved,
        method=method,
        fallback_reason=fallback_reason,
    )
