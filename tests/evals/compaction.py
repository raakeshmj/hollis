"""Compaction fidelity: does long-run compaction keep the evidence later turns depend on?

A scripted agent runs many turns. Early tool results contain planted facts (`FACT-<n>`), and
some turns perform side effects (`deploy`). History is compacted repeatedly. We measure what the
final model request still carries:

- actions: every side effect, listed in the summary ledger (rebuilt from events);
- notes: every planted fact that a summarizer recorded at some point;
- mandatory context: the original input in every request;
- budget: every recorded estimate within the input budget (when a window is declared).

Summarizers compared:
- `deterministic`: no model (ledger plus carried notes);
- `forgetful`: a model-like summarizer that records the facts it sees in the newly compacted
  turns but never repeats earlier ones; the carry-forward rule must keep them;
- `naive_replace` (simulated, not Hollis behaviour): what survives if each new summary simply
  replaced the previous one, as plain summary-of-summary compaction does when the model drops
  earlier content.
"""

import re
from dataclasses import dataclass
from typing import Any

from hollis.adapters.fake_provider import FAKE_DESCRIPTOR, ScriptedModelProvider
from hollis.adapters.memory_store import InMemoryRunStore
from hollis.runtime.assembly import history
from hollis.runtime.compaction import DeterministicSummarizer, Summarizer, SummarizeRequest
from hollis.runtime.context import (
    CompactionMethod,
    ContextPolicy,
    DraftItem,
    SummaryDraft,
    SummarySection,
    estimate_parts,
)
from hollis.runtime.conversation import render_context
from hollis.runtime.events import ContextAssembled
from hollis.runtime.model import ModelCapabilities, ModelResponse, TextCapabilities
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import Runner, RunSpec
from hollis.runtime.tools import ApprovalDecision, ApprovalRequest, ToolEffect, ToolOutput
from tests.support.tools import Catalog, FakeTool, answer, call, wants

FACT = re.compile(r"FACT-(\d+): [^\n]+")
INPUT = "Migrate the 12 regional clusters to the new ingress controller and report blockers."


class Forgetful:
    """Records facts seen in newly compacted turns; never mentions earlier ones again."""

    def __init__(self) -> None:
        self.drafts: list[SummaryDraft] = []

    @property
    def method(self) -> CompactionMethod:
        return CompactionMethod.MODEL

    async def summarize(self, request: SummarizeRequest) -> SummaryDraft:
        facts = dict.fromkeys(m.group(0) for m in FACT.finditer(request.transcript))
        draft = SummaryDraft(
            state=f"Working through turns up to {request.through_turn}.",
            new_items=tuple(
                DraftItem(section=SummarySection.OBSERVATION, text=fact[:900]) for fact in facts
            )[:20],
        )
        self.drafts.append(draft)
        return draft


class _Approve:
    async def review(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(binding=request.binding, approved=True, approver="eval")


@dataclass
class CompactionResult:
    summarizer: str
    turns: int
    compactions: int
    actions_kept: float
    notes_kept: float
    input_always_present: bool
    within_budget: bool
    final_tokens: int
    full_history_tokens: int
    drafts: tuple[SummaryDraft, ...] = ()


def _script(turns: int) -> list[ModelResponse]:
    script: list[ModelResponse] = []
    for i in range(turns):
        if i % 4 == 3:
            script.append(
                wants(
                    call("deploy", f"c{i}", cluster=f"region-{i}"), content=f"Deploying region-{i}."
                )
            )
        else:
            script.append(
                wants(
                    call("inspect", f"c{i}", cluster=f"region-{i}"),
                    content=f"Inspecting region-{i}.",
                )
            )
    script.append(answer("Migration complete; blockers listed above."))
    return script


async def run_compaction(
    summarizer_name: str, *, turns: int = 32, window: int | None = 12_000
) -> CompactionResult:
    async def inspect(arguments: dict[str, Any], context: object) -> ToolOutput:
        cluster = str(arguments["cluster"])
        number = int(cluster.split("-")[1])
        filler = "\n".join(f"{cluster} pod-{k} Running 0 restarts" for k in range(40))
        return ToolOutput.text(
            f"FACT-{number}: {cluster} ingress uses TLS policy tls-{number % 3}\n{filler}"
        )

    async def deploy(arguments: dict[str, Any], context: object) -> ToolOutput:
        return ToolOutput.text(f"deployed ingress v2 to {arguments['cluster']}")

    tools = Catalog(
        FakeTool("inspect", handler=inspect),
        FakeTool("deploy", effect=ToolEffect.MUTATING, handler=deploy),
    )
    descriptor = FAKE_DESCRIPTOR.model_copy(
        update={
            "capabilities": ModelCapabilities(
                text=TextCapabilities(tools=True, context_window_tokens=window)
            )
        }
    )
    provider = ScriptedModelProvider(_script(turns), descriptor=descriptor)
    summarizer: Summarizer = (
        Forgetful() if summarizer_name == "forgetful" else DeterministicSummarizer()
    )
    store = InMemoryRunStore()
    runner = Runner(store, provider, tools=tools, approver=_Approve(), summarizer=summarizer)
    run = await runner.create(
        RunSpec(
            model="fake",
            input=INPUT,
            tools=("inspect", "deploy"),
            budget=RunBudget(
                max_output_tokens=500, max_model_turns=turns + 2, max_tool_calls=turns + 2
            ),
            context=ContextPolicy(max_history_turns=6, keep_recent_turns=2),
        )
    )
    final = await runner.execute(run.run_id)
    assert final.status.value == "completed", final.failure

    last = provider.requests[-1]
    context_text = render_context(last.context)
    side_effects = [f"region-{i}" for i in range(turns) if i % 4 == 3]
    verbatim = {  # side effects still in the verbatim window count as kept
        str(c.arguments.get("cluster") if c.arguments else "")
        for item in last.history
        if item.type == "assistant"
        for c in item.tool_calls
    }
    actions = sum(
        1
        for c in side_effects
        if f'deploy({{"cluster": "{c}"}}) -> success' in context_text or c in verbatim
    )
    drafts = summarizer.drafts if isinstance(summarizer, Forgetful) else []
    noted = {m.group(1) for d in drafts for i in d.new_items if (m := FACT.match(i.text))}
    notes = sum(1 for n in noted if f"FACT-{n}:" in context_text) / len(noted) if noted else 1.0
    plans = [
        e.payload for e in await store.read(run.run_id) if isinstance(e.payload, ContextAssembled)
    ]
    within = all(
        p.budget.input_tokens is None or p.estimate.input_tokens <= p.budget.input_tokens
        for p in plans
    )
    full = estimate_parts(
        instructions=None, context=None, input_text=INPUT, history=history(final), tools=last.tools
    )
    return CompactionResult(
        summarizer=summarizer_name,
        turns=turns,
        compactions=len(final.compactions),
        actions_kept=actions / len(side_effects),
        notes_kept=notes,
        input_always_present=all(r.input == INPUT for r in provider.requests),
        within_budget=within,
        final_tokens=plans[-1].estimate.input_tokens,
        full_history_tokens=full,
        drafts=tuple(summarizer.drafts) if isinstance(summarizer, Forgetful) else (),
    )


def naive_replace_notes_kept(drafts: tuple[SummaryDraft, ...]) -> float:
    """Fraction of ever-recorded facts that survive if each summary replaced the previous one."""
    noted = {i.text for d in drafts for i in d.new_items}
    survivors: set[str] = {i.text for i in drafts[-1].new_items} if drafts else set()
    return len(survivors) / len(noted) if noted else 1.0


def markdown(results: list[CompactionResult], naive: float) -> str:
    lines = [
        "| summarizer | turns | compactions | side effects kept | notes kept "
        "| input in every request | within budget | final tokens | uncompacted history tokens |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.summarizer} | {r.turns} | {r.compactions} | {r.actions_kept:.0%} "
            f"| {r.notes_kept:.0%} "
            f"| {'yes' if r.input_always_present else 'NO'} | {'yes' if r.within_budget else 'NO'} "
            f"| {r.final_tokens} | {r.full_history_tokens} |"
        )
    lines.append(f"| naive_replace (simulated) | | | n/a | {naive:.0%} | | | | |")
    return "\n".join(lines)
