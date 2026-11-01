"""Multi-agent orchestration on durable runs (ADR-0030, ADR-0031).

A delegation is a tool call whose execution is a child run. An agent with `delegates` gets one
`delegate_<agent>` tool per delegate; calling it:

1. derives the child's run id from (parent run, invocation), so re-executing the call after a
   crash attaches to the child instead of creating a second one (the store refuses a second
   `RunCreated` for an id): the tool is idempotent, and recovery re-executes it safely;
2. checks the limits recorded on the parent (depth, children, runs per tree, token budget) and
   that the run's owner may still run agents;
3. narrows permissions: the child's memory and knowledge are its agent's configuration, within
   the owner's current grants, within the parent run's access. Tool calls are decided by the
   workspace policy like any run's, with the delegation depth as an extra fact;
4. creates and schedules the child, then waits for it (an approval it needs is decided like any
   other, by someone allowed to approve on the child run);
5. returns a report built from the child's *log*: the outcome Hollis observed, usage, tool calls
   with unknown outcome, the grounding check of its answer, the evidence it cited (rendered from
   recorded sources, never from its text), its self-reported status, and its answer framed as
   untrusted data.

`Orchestrator` also provides the tree-wide token budget check (a `ModelCallGuard`), cascading
cancellation and the orchestration view.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from hollis.adapters.credentials import MissingCredential
from hollis.gateway.gateway import RoutingDenied, WrongTask
from hollis.knowledge.tools import handle as evidence_handle
from hollis.platform.auth import Principal
from hollis.platform.schemas import (
    Agent,
    DisagreementView,
    OrchestrationNode,
    OrchestrationTotals,
    OrchestrationView,
    RefusalView,
    ReportView,
    Role,
    RunPhase,
    RunSummary,
    VerificationView,
)
from hollis.runtime.delegation import (
    REPORT_KIND,
    Assignment,
    DelegationLimits,
    Evidence,
    InputRef,
    Lineage,
    child_run_id,
    extract_reports,
    report_block,
    report_meta,
)
from hollis.runtime.events import FailureReason, ToolOutcome
from hollis.runtime.grounding import CITATION, Support, neutralize, tool_evidence
from hollis.runtime.knowledge import KnowledgeAccess
from hollis.runtime.memory import MemoryAccess
from hollis.runtime.model import CapabilityMismatch, UnknownModel
from hollis.runtime.policy import RunBudget
from hollis.runtime.results import render_parts
from hollis.runtime.runner import RunSpec, SecretInRunInput, UnknownTool
from hollis.runtime.state import RunState, RunStatus
from hollis.runtime.store import ConcurrencyConflict, RunNotFound, load_run
from hollis.runtime.tools import (
    JsonPart,
    TextPart,
    ToolArgumentError,
    ToolContext,
    ToolDescriptor,
    ToolEffect,
    ToolOutput,
    ToolTrust,
)

if TYPE_CHECKING:
    from hollis.platform.workspace import Workspace

log = logging.getLogger("hollis.platform")

DELEGATE_PREFIX = "delegate_"
WAIT_MARGIN_S = 2.0
"""A child must finish this long before the parent's own deadline, so the parent can record it."""
POLL_S = 5.0
EVIDENCE_BLOCKS = 6
EVIDENCE_CHARS = 700
MIN_ANSWER_CHARS = 400
DELEGATION_SHARE = 0.75
"""New delegations stop once a token budget is this much used, so the runs that delegated keep
the rest to report what they have. Their own model calls stop at the full budget."""

REPORT_PROTOCOL = """Report protocol (Hollis): another agent delegated this task to you. Work only \
on the objective, with the tools and evidence you have. Cite evidence you rely on by its handle, \
for example [S1] or [C-1a2b3c4d]. Say plainly what you could not do or verify; do not guess. End \
your reply with a JSON block:
```json
{"status": "done", "answer": "<one-line answer>", "confidence": "medium", "open_questions": []}
```
status is done, partial, blocked or needs_input; confidence is high, medium or low."""

INPUT_SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "objective": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4000,
            "description": "One bounded, self-contained task: what to find out or do.",
        },
        "context": {
            "type": "string",
            "maxLength": 20000,
            "description": "Facts and constraints the agent needs. It sees nothing else of "
            "your conversation.",
        },
        "expected_output": {
            "type": "string",
            "maxLength": 2000,
            "description": "What a useful answer looks like.",
        },
        "inputs": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 8,
            "description": "Earlier delegations of yours (tool call ids) whose reports this "
            "agent should receive verbatim.",
        },
    },
    "required": ["objective"],
    "additionalProperties": False,
}


def delegate_tool_name(agent: str) -> str:
    return DELEGATE_PREFIX + agent.replace("-", "_")


def delegated_agent(tool: str) -> str | None:
    """The agent a delegate tool targets (agent names never contain underscores)."""
    if not tool.startswith(DELEGATE_PREFIX):
        return None
    return tool.removeprefix(DELEGATE_PREFIX).replace("_", "-")


def _now() -> datetime:
    return datetime.now(UTC)


# --- grants ------------------------------------------------------------------------------------


def effective_memory(configured: MemoryAccess | None, p: Principal) -> MemoryAccess | None:
    """An agent's memory configuration within a principal's grants (admins hold every grant)."""
    if configured is None or p.is_admin:
        return configured
    return narrow_memory(configured, p.memory or MemoryAccess())[0]


def effective_knowledge(configured: KnowledgeAccess | None, p: Principal) -> KnowledgeAccess | None:
    if configured is None or p.is_admin:
        return configured
    return narrow_knowledge(configured, p.knowledge or KnowledgeAccess())[0]


def narrow_memory(
    configured: MemoryAccess | None, ceiling: MemoryAccess | None
) -> tuple[MemoryAccess | None, list[str]]:
    """`configured` within `ceiling`, and what was removed. A scope is writable only if it is
    also readable."""
    if configured is None:
        return None, []
    ceiling = ceiling or MemoryAccess()
    read = tuple(s for s in configured.read if s in ceiling.read)
    write = tuple(s for s in configured.write if s in ceiling.write and s in read)
    removed = [f"memory read {s}" for s in configured.read if s not in read]
    removed += [f"memory write {s}" for s in configured.write if s not in write]
    return (MemoryAccess(read=read, write=write) if read else None), removed


def narrow_knowledge(
    configured: KnowledgeAccess | None, ceiling: KnowledgeAccess | None
) -> tuple[KnowledgeAccess | None, list[str]]:
    if configured is None:
        return None, []
    ceiling = ceiling or KnowledgeAccess()
    collections = tuple(c for c in configured.collections if c in ceiling.collections)
    labels = tuple(label for label in configured.labels if label in ceiling.labels)
    removed = [f"knowledge {c}" for c in configured.collections if c not in collections]
    removed += [f"label {label}" for label in configured.labels if label not in labels]
    access = KnowledgeAccess(collections=collections, labels=labels) if collections else None
    return access, removed


_EFFECT_RANK = {ToolEffect.READ_ONLY: 0, ToolEffect.MUTATING: 1, ToolEffect.DESTRUCTIVE: 2}


def delegation_effect(
    agent: str,
    agents: Mapping[str, Agent],
    tool_effect: Callable[[str], ToolEffect | None],
    _seen: frozenset[str] = frozenset(),
) -> ToolEffect:
    """The strongest effect of anything delegating to `agent` can cause: its tools and, in
    turn, its delegates'. Unknown tools (including any MCP tool) count as mutating."""
    if agent in _seen:
        return ToolEffect.READ_ONLY  # a cycle adds nothing new
    definition = agents.get(agent)
    if definition is None:
        return ToolEffect.MUTATING
    effects = [tool_effect(t) or ToolEffect.MUTATING for t in definition.tools]
    effects += [
        delegation_effect(d, agents, tool_effect, _seen | {agent}) for d in definition.delegates
    ]
    return max(effects, key=_EFFECT_RANK.__getitem__, default=ToolEffect.READ_ONLY)


# --- the delegate tool ------------------------------------------------------------------------


class DelegateArguments(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    objective: str = Field(min_length=1, max_length=4000)
    context: str = Field(default="", max_length=20_000)
    expected_output: str = Field(default="", max_length=2000)
    inputs: tuple[str, ...] = Field(default=(), max_length=8)


class DelegateTool:
    """`delegate_<agent>`: run one bounded task as a child run of that agent."""

    def __init__(self, orchestrator: "Orchestrator", agent: Agent, effect: ToolEffect) -> None:
        self._orchestrator = orchestrator
        self._agent = agent.name
        about = f" ({agent.description.strip()})" if agent.description.strip() else ""
        self._descriptor = ToolDescriptor(
            name=delegate_tool_name(agent.name),
            version="1",
            description=(
                f"Delegate one bounded task to the `{agent.name}` agent{about}. It works on its "
                "own, with its own tools, and returns a report: its answer (model output, not "
                "verified), a check of its citations, and the evidence it used. Give a clear "
                "objective and the context it needs. Delegate independent tasks in the same turn; "
                "pass earlier results with `inputs` instead of retyping them."
            ),
            input_schema=INPUT_SCHEMA,
            effect=effect,
            idempotent=True,  # re-execution attaches to the same child run
            trust=ToolTrust.TRUSTED,  # the result's structure is Hollis's; the answer is framed
            timeout_s=3600,
        )

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._descriptor

    def validate(self, arguments: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        try:
            parsed = DelegateArguments.model_validate(dict(arguments))
        except ValidationError as exc:
            error = exc.errors()[0]
            where = ".".join(str(part) for part in error["loc"]) or "arguments"
            raise ToolArgumentError(f"{where}: {error['msg']}") from None
        return cast(dict[str, JsonValue], parsed.model_dump(mode="json"))

    async def invoke(self, arguments: dict[str, JsonValue], context: ToolContext) -> ToolOutput:
        parsed = DelegateArguments.model_validate(arguments)
        return await self._orchestrator.delegate(self._agent, parsed, context)

    async def reconcile(self, context: ToolContext) -> ToolOutput | None:
        return None


# --- reports -----------------------------------------------------------------------------------


_SELF_REPORT = re.compile(r"```json\s*(\{.*?\})\s*```\s*$", re.S)
_STATUSES = ("done", "partial", "blocked", "needs_input")
_CONFIDENCE = ("high", "medium", "low")


def parse_self_report(text: str) -> tuple[str, dict[str, Any]]:
    """Split a child's answer into its prose and its (validated) JSON self-report."""
    match = _SELF_REPORT.search(text.strip())
    if match is None:
        return text.strip(), {}
    try:
        raw: object = json.loads(match.group(1))
    except ValueError:
        return text.strip(), {}
    if not isinstance(raw, dict):
        return text.strip(), {}
    data = cast(dict[str, Any], raw)
    report: dict[str, Any] = {}
    if data.get("status") in _STATUSES:
        report["status"] = data["status"]
    if data.get("confidence") in _CONFIDENCE:
        report["confidence"] = data["confidence"]
    if isinstance(data.get("answer"), str) and data["answer"].strip():
        report["answer"] = neutralize(" ".join(data["answer"].split()))[:300]
    questions = data.get("open_questions")
    if isinstance(questions, list):
        items = cast(list[object], questions)
        report["open_questions"] = [
            neutralize(q)[:300] for q in items if isinstance(q, str) and q.strip()
        ][:5]
    return text.strip()[: match.start()].rstrip(), report


def normalize_answer(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def disagreements(items: Sequence[tuple[str, UUID, str]]) -> list[DisagreementView]:
    """Groups of (objective, run, answer) asked the same objective that answered differently."""
    groups: dict[str, list[tuple[str, UUID, str]]] = {}
    for objective, run_id, answer in items:
        groups.setdefault(normalize_answer(objective), []).append((objective, run_id, answer))
    found: list[DisagreementView] = []
    for members in groups.values():
        if len(members) > 1 and len({normalize_answer(a) for _, _, a in members}) > 1:
            found.append(
                DisagreementView(
                    objective=members[0][0],
                    runs=tuple(r for _, r, _ in members),
                    answers=tuple(a for _, _, a in members),
                )
            )
    return found


def _outcome(child: RunState, timed_out: bool) -> tuple[str, str | None]:
    if child.status is RunStatus.COMPLETED:
        return "completed", None
    if child.status is RunStatus.FAILED and child.failure is not None:
        if child.failure.reason is FailureReason.BUDGET_EXHAUSTED:
            return "budget_exhausted", child.failure.message
        return "failed", f"{child.failure.reason.value}: {child.failure.message}"
    if child.cancellation is not None:
        return ("timed_out" if timed_out else "cancelled"), child.cancellation.reason
    return "failed", f"stopped in status {child.status.value}"


def _unknown_side_effects(child: RunState) -> list[str]:
    unknown: list[str] = []
    interrupted: set[str] = set(child.cancellation.interrupted) if child.cancellation else set()
    for turn in child.turns:
        for invocation in turn.invocations:
            name = invocation.call.name
            if invocation.outcome is ToolOutcome.AMBIGUOUS or invocation.invocation_id in (
                interrupted
            ):
                unknown.append(name)
            elif name.startswith(DELEGATE_PREFIX) and invocation.parts:
                meta = report_meta(render_parts(invocation.parts)) or {}
                unknown += [
                    f"{meta.get('agent')}: {x}" for x in meta.get("side_effects_unknown", [])
                ]
    return unknown


def _cited_evidence(child: RunState) -> dict[str, tuple[str, str]]:
    """handle -> (source, text) for the evidence the child's answer cited, rendered from what
    its log recorded (retrieved passages, trusted tool results, evidence handed to it)."""
    if child.grounding is None:
        return {}
    available: dict[str, tuple[str, str]] = {}
    for passage in child.passages:
        source = " | ".join(
            x for x in (passage.collection, passage.source_title, passage.location) if x
        )
        available[passage.key] = (source, neutralize(passage.text))
    for turn in child.turns:
        for invocation in turn.invocations:
            ref = child.tool(invocation.call.name)
            if ref is None or ref.trust is not ToolTrust.TRUSTED or not invocation.parts:
                continue
            for key, text in tool_evidence(render_parts(invocation.parts)).items():
                available.setdefault(key, (f"via {invocation.call.name}", text))
    if child.assignment is not None:
        for item in child.assignment.evidence:
            available.setdefault(item.handle, (item.source, item.text))
    cited: dict[str, tuple[str, str]] = {}
    handles = {p.key: evidence_handle(p.chunk_id) for p in child.passages}
    for key in child.grounding.cited:
        if key in available:
            cited[handles.get(key, key)] = available[key]
    return cited


def _with_handles(text: str, child: RunState) -> str:
    """Rewrite the child's S-keys (local to its run) to stable C-handles."""
    handles = {p.key: evidence_handle(p.chunk_id) for p in child.passages}
    if not handles:
        return text
    return CITATION.sub(
        lambda m: (
            "["
            + ", ".join(handles.get(k.strip(), k.strip()) for k in re.split(r"[,;]", m.group(1)))
            + "]"
        ),
        text,
    )


def build_report(child: RunState, agent: str, *, limit: int, timed_out: bool = False) -> ToolOutput:
    outcome, reason = _outcome(child, timed_out)
    answer = child.response.content if outcome == "completed" and child.response else ""
    prose, claimed = parse_self_report(answer)
    prose = neutralize(_with_handles(prose, child)) if prose else ""
    if outcome != "completed":
        prose = f"The run did not complete ({outcome}): {reason or 'no reason recorded'}."
    evidence = _cited_evidence(child)
    grounding = child.grounding
    unknown = _unknown_side_effects(child)
    calls = sum(len(t.invocations) for t in child.turns)
    meta: dict[str, JsonValue] = {
        "kind": REPORT_KIND,
        "agent": agent,
        "run_id": str(child.run_id),
        "depth": child.depth,
        "outcome": outcome,
        "reason": reason,
        **claimed,
        "verification": {
            "claims": len(grounding.claims),
            "supported": grounding.count(Support.SUPPORTED),
            "weak": grounding.count(Support.WEAK),
            "uncited": grounding.count(Support.UNCITED),
            "fabricated": len(grounding.fabricated),
        }
        if grounding is not None
        else None,
        "side_effects_unknown": cast(list[JsonValue], unknown),
        "usage": {
            "input_tokens": child.usage.input_tokens,
            "output_tokens": child.usage.output_tokens,
            "turns": len(child.turns),
            "tool_calls": calls,
        },
        "evidence": cast(list[JsonValue], list(evidence)),
    }
    header = [
        f"Delegation report: agent `{agent}`, run {child.run_id}, outcome {outcome} "
        f"({len(child.turns)} turns, {calls} tool calls, {child.total_tokens} tokens)."
    ]
    if claimed.get("status") or claimed.get("confidence"):
        header.append(
            f"Self-reported: status {claimed.get('status', 'not given')}, "
            f"confidence {claimed.get('confidence', 'not given')}."
        )
    if grounding is not None and grounding.claims:
        header.append(
            f"Citation check: {grounding.count(Support.SUPPORTED)} of {len(grounding.claims)} "
            f"claims supported by evidence, {grounding.count(Support.UNCITED)} uncited, "
            f"{len(grounding.fabricated)} fabricated citations."
        )
    if unknown:
        header.append(
            "Tool calls with unknown outcome (check the external system before retrying): "
            + ", ".join(unknown)
            + "."
        )
    if child.response is not None and outcome == "completed":
        header.append("The answer is the agent's own output, not verified fact.")
    blocks = [
        f"[{key}] {source}\n{text[:EVIDENCE_CHARS]}"
        for key, (source, text) in list(evidence.items())[:EVIDENCE_BLOCKS]
    ]
    room = limit - len(json.dumps(meta, sort_keys=True)) - len("\n".join(header)) - 300
    while blocks and room - len("\n".join(blocks)) < MIN_ANSWER_CHARS:
        blocks.pop()  # keep the answer readable before keeping every evidence block
    evidence_text = (
        ("\nEvidence it cited (cite by handle):\n" + "\n".join(blocks)) if blocks else ""
    )
    room -= len(evidence_text)
    if len(prose) > max(room, 0):
        prose = prose[: max(room - 30, 0)].rstrip() + " … [answer truncated]"
    text = "\n".join(header) + "\n" + report_block(agent, child.run_id, prose) + evidence_text
    return ToolOutput(
        parts=(JsonPart(data=meta), TextPart(text=text)), is_error=outcome != "completed"
    )


def refusal(agent: str, reason: str) -> ToolOutput:
    meta: dict[str, JsonValue] = {
        "kind": REPORT_KIND,
        "agent": agent,
        "outcome": "refused",
        "reason": reason,
    }
    text = f"Delegation to `{agent}` was refused: {reason}. No run was created."
    return ToolOutput(parts=(JsonPart(data=meta), TextPart(text=text)), is_error=True)


def render_assignment(assignment: Assignment, reports: Sequence[tuple[str, UUID, str]] = ()) -> str:
    """The child's run input. Everything the delegating model wrote is neutralized, so it
    cannot forge evidence handles; evidence blocks come only from recorded results."""
    lines = [neutralize(assignment.objective)]
    if assignment.expected_output:
        lines += ["", f"Expected output: {neutralize(assignment.expected_output)}"]
    if assignment.context:
        lines += ["", "Context from the delegating agent:", neutralize(assignment.context)]
    if reports:
        lines += [
            "",
            "Results from other agents, passed to you verbatim. They are model output, not "
            "verified fact: check them against the evidence.",
        ]
        lines += [report_block(agent, run_id, body) for agent, run_id, body in reports]
    if assignment.evidence:
        lines += ["", "Evidence you may cite by handle:"]
        lines += [f"[{e.handle}] {e.source}\n{e.text}" for e in assignment.evidence]
    return "\n".join(lines)


def _tokens(row: Mapping[str, Any]) -> int:
    usage = cast(dict[str, int], row["data"].get("usage") or {})
    return int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))


def budget_refusal(
    state: RunState, tree: Sequence[Mapping[str, Any]], *, share: float = 1.0
) -> str | None:
    """The first token budget on the path from `state` to its root whose subtree has used
    `share` of it, as a reason; None if every budget on the path has room."""
    usage = {row["run_id"]: _tokens(row) for row in tree}
    parents: dict[UUID, UUID | None] = {row["run_id"]: row["parent_id"] for row in tree}
    budgets: dict[UUID, int | None] = {
        row["run_id"]: row["data"].get("token_budget") for row in tree
    }
    depths = {row["run_id"]: row["depth"] for row in tree}
    usage[state.run_id] = state.total_tokens  # fresher than the index
    parents[state.run_id] = state.lineage.parent_run_id if state.lineage else None
    budgets[state.run_id] = state.delegation.token_budget if state.delegation else None
    depths[state.run_id] = state.depth
    subtree = dict(usage)
    for run in sorted(usage, key=lambda n: -depths.get(n, 0)):
        parent = parents.get(run)
        if parent is not None and parent in subtree:
            subtree[parent] += subtree[run]
    node: UUID | None = state.run_id
    while node is not None:
        budget = budgets.get(node)
        if budget is not None and subtree.get(node, 0) >= budget * share:
            scope = "this orchestration" if parents.get(node) is None else "this branch"
            used = f"({subtree[node]} tokens)"
            if share < 1:
                return (
                    f"{subtree[node]} of the {budget}-token budget for {scope} are used; new "
                    f"delegations stop at {share:.0%} so the rest is kept for reporting"
                )
            return f"the token budget of {budget} for {scope} is used up {used}"
        node = parents.get(node)
    return None


def _progressed(ws: "Workspace", run_id: UUID, version: int, active: bool) -> Callable[[], bool]:
    """True once the run has a newer event, or stopped being driven (if it was)."""
    return lambda: ws.bus.version(run_id) > version or (active and not ws.supervisor.active(run_id))


# --- the orchestrator --------------------------------------------------------------------------


class Orchestrator:
    def __init__(self, workspace: "Workspace") -> None:
        self._ws = workspace
        self._locks: dict[UUID, asyncio.Lock] = {}

    def limits(self, agent: Agent | None) -> DelegationLimits:
        """Workspace limits narrowed by an agent's own."""
        base = DelegationLimits.model_validate(self._ws.config.orchestration.model_dump())
        return base.narrow(agent.delegation if agent is not None else None)

    def tool_names(
        self, agent: Agent, *, depth: int, limits: DelegationLimits | None
    ) -> tuple[str, ...]:
        """The delegate tools a run of `agent` at `depth` gets (none at the depth limit)."""
        if limits is None or depth >= limits.max_depth or limits.max_children == 0:
            return ()
        return tuple(delegate_tool_name(d) for d in agent.delegates if d in self._ws.agents)

    # --- delegation -----------------------------------------------------------------------------

    async def delegate(self, agent: str, args: DelegateArguments, ctx: ToolContext) -> ToolOutput:
        child_id = child_run_id(ctx.run_id, ctx.invocation_id)
        parent = await load_run(self._ws.runs, ctx.run_id)
        limits = parent.delegation
        if limits is None:
            return refusal(agent, "this run may not delegate")
        root_id = parent.lineage.root_run_id if parent.lineage else parent.run_id
        if not await self._exists(child_id):
            async with self._locks.setdefault(root_id, asyncio.Lock()):  # counts, budgets
                if not await self._exists(child_id):
                    refused = await self._create(agent, args, ctx, parent, root_id, limits)
                    if refused is not None:
                        log.info(
                            "run %s: delegation to %s refused: %s", parent.run_id, agent, refused
                        )
                        return refusal(agent, refused)
        await self._adopt(child_id, parent)
        child, timed_out = await self._wait(child_id, limits, ctx)
        limit = min(
            parent.context_policy.artifact_threshold_chars, parent.budget.max_tool_output_chars
        )
        return build_report(child, agent, limit=limit, timed_out=timed_out)

    async def _exists(self, run_id: UUID) -> bool:
        try:
            await self._ws.runs.read(run_id)
        except RunNotFound:
            return False
        return True

    async def _create(
        self,
        agent_name: str,
        args: DelegateArguments,
        ctx: ToolContext,
        parent: RunState,
        root_id: UUID,
        limits: DelegationLimits,
    ) -> str | None:
        """Create and schedule the child, or return why not."""
        ws = self._ws
        agent = ws.agents.get(agent_name)
        if agent is None:
            return f"there is no agent {agent_name!r}"
        depth = parent.depth + 1
        if depth > limits.max_depth:
            return f"depth {depth} would exceed the limit of {limits.max_depth}"
        parent_row = await ws.db.index_get(parent.run_id)
        owner_name = parent_row["created_by"] if parent_row else None
        owner = await ws.auth.principal(owner_name) if owner_name else None
        if owner is None:
            return "the run's owner is unknown or no longer exists"
        if owner.role is Role.VIEWER:
            return f"{owner.name} may no longer run agents"
        children = await ws.db.index_children(parent.run_id)
        if len(children) >= limits.max_children:
            return f"this run already delegated {len(children)} tasks (limit {limits.max_children})"
        tree = await ws.db.index_tree(root_id)
        if len(tree) >= limits.max_runs:
            return f"the orchestration already has {len(tree)} runs (limit {limits.max_runs})"
        exhausted = budget_refusal(parent, tree, share=DELEGATION_SHARE)
        if exhausted is not None:
            return exhausted
        resolved = self._inputs(parent, args.inputs)
        if isinstance(resolved, str):
            return resolved
        reports, refs, evidence = resolved
        # The ceiling is the parent run's access within the owner's current grants (which may
        # have shrunk since the root started); the child gets its agent's configuration within it.
        memory_ceiling, knowledge_ceiling = parent.memory_access, parent.knowledge_access
        if not owner.is_admin:
            memory_ceiling = narrow_memory(memory_ceiling, owner.memory)[0]
            knowledge_ceiling = narrow_knowledge(knowledge_ceiling, owner.knowledge)[0]
        memory, lost_memory = narrow_memory(agent.memory, memory_ceiling)
        knowledge, lost_knowledge = narrow_knowledge(agent.knowledge, knowledge_ceiling)
        child_limits = limits.narrow(agent.delegation)
        tools = (*agent.tools, *self.tool_names(agent, depth=depth, limits=child_limits))
        budget = agent.budget or RunBudget(max_output_tokens=ws.config.runtime.max_output_tokens)
        if parent.deadline is not None:
            remaining = (parent.deadline - _now()).total_seconds() - WAIT_MARGIN_S
            if remaining <= 1:
                return "the delegating run's deadline has passed"
            deadline = min(budget.deadline_s or remaining, remaining)
            budget = budget.model_copy(update={"deadline_s": deadline})
        assignment = Assignment(
            agent=agent.name,
            objective=args.objective,
            context=args.context,
            expected_output=args.expected_output,
            inputs=refs,
            evidence=evidence,
        )
        instructions = "\n\n".join(x for x in (agent.instructions.strip(), REPORT_PROTOCOL) if x)
        spec = RunSpec(
            model=agent.model,
            input=render_assignment(assignment, reports),
            instructions=instructions,
            budget=budget,
            tools=tools,
            context=agent.context,
            memory=memory,
            knowledge=knowledge,
            lineage=Lineage(
                root_run_id=root_id,
                parent_run_id=parent.run_id,
                parent_invocation_id=ctx.invocation_id,
                depth=depth,
            ),
            assignment=assignment,
            delegation=child_limits,
        )
        child_id = child_run_id(parent.run_id, ctx.invocation_id)
        try:
            await ws.connect_tools(tools)
            await ws.runner.create(spec, run_id=child_id)
        except ConcurrencyConflict:
            return None  # created meanwhile: attach to it
        except (
            UnknownModel,
            RoutingDenied,
            WrongTask,
            CapabilityMismatch,
            UnknownTool,
            SecretInRunInput,
            MissingCredential,
            ValueError,
        ) as exc:
            return f"the {agent.name} agent cannot run: {ws.redactor.redact(str(exc))[:300]}"
        await ws.db.index_set_owner(child_id, agent=agent.name, created_by=owner.name, session=None)
        patch: dict[str, Any] = {"narrowed": [*lost_memory, *lost_knowledge]}
        if agent.record_episodes and memory is not None and memory.write:
            patch["episode_scope"] = memory.write[0]
        await ws.db.index_patch(child_id, patch)
        await ws.db.audit(
            owner.name, "run.delegate", str(child_id), f"{parent.run_id} -> {agent.name}"
        )
        log.info("run %s: delegated to %s as run %s", parent.run_id, agent.name, child_id)
        ws.supervisor.schedule(child_id)
        return None

    def _inputs(
        self, parent: RunState, refs: Sequence[str]
    ) -> tuple[list[tuple[str, UUID, str]], tuple[InputRef, ...], tuple[Evidence, ...]] | str:
        """Earlier results passed to a child: finished delegations of the same parent only."""
        finished = [
            invocation
            for turn in parent.turns
            for invocation in turn.invocations
            if invocation.call.name.startswith(DELEGATE_PREFIX) and invocation.outcome is not None
        ]
        reports: list[tuple[str, UUID, str]] = []
        inputs: list[InputRef] = []
        evidence: dict[str, Evidence] = {}
        for ref in dict.fromkeys(refs):
            match = next(
                (
                    i
                    for i in finished
                    if ref
                    in (
                        i.call.call_id,
                        i.invocation_id,
                        str(child_run_id(parent.run_id, i.invocation_id)),
                    )
                ),
                None,
            )
            if match is None:
                return (
                    f"inputs: {ref!r} is not a finished delegation of this run (pass the tool "
                    "call id of an earlier delegation; ones in the same turn are not finished)"
                )
            text = render_parts(match.parts)
            meta = report_meta(text) or {}
            if meta.get("outcome") == "refused":
                return f"inputs: {ref!r} was refused, so it has no result"
            run_id = child_run_id(parent.run_id, match.invocation_id)
            agent = str(meta.get("agent") or "agent")
            reports += [(agent, run_id, body) for _, _, body in extract_reports(text)]
            inputs.append(InputRef(run_id=run_id, agent=agent))
            for key, body in tool_evidence(text).items():
                evidence.setdefault(
                    key, Evidence(handle=key, text=body[:4000], source=f"cited by {agent}")
                )
        return reports, tuple(inputs), tuple(list(evidence.values())[:32])

    async def _adopt(self, child_id: UUID, parent: RunState) -> None:
        """Record the child's owner if a crash came between creating and indexing it."""
        row = await self._ws.db.index_get(child_id)
        if row is None or row["created_by"] is not None:
            return
        parent_row = await self._ws.db.index_get(parent.run_id)
        child = await load_run(self._ws.runs, child_id)
        if parent_row is not None and parent_row["created_by"] is not None:
            agent = child.assignment.agent if child.assignment else None
            await self._ws.db.index_set_owner(
                child_id, agent=agent, created_by=parent_row["created_by"], session=None
            )

    async def _wait(
        self, child_id: UUID, limits: DelegationLimits, ctx: ToolContext
    ) -> tuple[RunState, bool]:
        """Wait until the child is terminal. A child that cannot finish in time, or whose
        execution stopped with an error, is cancelled, so no child outlives its delegation."""
        ws = self._ws
        limit = limits.wait_s
        if ctx.deadline is not None:
            limit = min(limit, (ctx.deadline - _now()).total_seconds() - WAIT_MARGIN_S)
        end = time.monotonic() + max(limit, 0.0)
        while True:
            state = await load_run(ws.runs, child_id)
            if state.status.is_terminal:
                return state, False
            remaining = end - time.monotonic()
            if remaining <= 0:
                waiting = ""
                if state.awaiting_approval:
                    names = ", ".join(i.call.name for i in state.awaiting_approval)
                    waiting = f" while it waited for approval of {names}"
                reason = f"the delegating run stopped waiting after {limit:.0f}s{waiting}"
                return await ws.supervisor.cancel(child_id, by="hollis", reason=reason), True
            active = ws.supervisor.active(child_id)
            if not active and not state.awaiting_approval:
                row = await ws.db.index_get(child_id)
                stopped = row["data"].get("stopped") if row else None
                if stopped:
                    reason = f"its execution stopped unexpectedly ({stopped})"
                    return await ws.supervisor.cancel(child_id, by="hollis", reason=reason), False
                active = ws.supervisor.schedule(child_id) or ws.supervisor.active(child_id)
            await ws.bus.wait_until(
                _progressed(ws, child_id, state.version, active), min(remaining, POLL_S)
            )

    # --- budgets (ModelCallGuard) ---------------------------------------------------------------

    async def refuse(self, state: RunState) -> str | None:
        limits = state.delegation
        if limits is None or limits.token_budget is None:
            return None  # budgets are inherited, so no ancestor has one either
        root_id = state.lineage.root_run_id if state.lineage else state.run_id
        return budget_refusal(state, await self._ws.db.index_tree(root_id))

    # --- cancellation ---------------------------------------------------------------------------

    async def descendants(self, run_id: UUID) -> list[UUID]:
        """Every run delegated below `run_id`, deepest first."""
        levels: list[list[UUID]] = []
        frontier = [run_id]
        while frontier:
            children = [
                row["run_id"]
                for parent in frontier
                for row in await self._ws.db.index_children(parent)
            ]
            if children:
                levels.append(children)
            frontier = children
        return [run for level in reversed(levels) for run in level]

    # --- views ----------------------------------------------------------------------------------

    async def view(
        self,
        focus: UUID,
        *,
        can_view: Callable[[Mapping[str, Any]], bool],
        summarize: Callable[[Mapping[str, Any]], RunSummary],
    ) -> OrchestrationView:
        ws = self._ws
        row = await ws.db.index_get(focus)
        if row is None:
            raise RunNotFound(focus)
        root_id = row["root_id"] or focus
        rows = await ws.db.index_tree(root_id)
        visible = [r for r in rows if can_view(r)]
        states = {r["run_id"]: await load_run(ws.runs, r["run_id"]) for r in visible}
        summaries = {r["run_id"]: summarize(r) for r in visible}
        reports: dict[UUID, dict[str, Any]] = {}
        refusals: list[RefusalView] = []
        for state in states.values():
            for turn in state.turns:
                for invocation in turn.invocations:
                    if not invocation.call.name.startswith(DELEGATE_PREFIX) or not invocation.parts:
                        continue
                    meta = report_meta(render_parts(invocation.parts))
                    if meta is None:
                        continue
                    if meta.get("outcome") == "refused":
                        refusals.append(
                            RefusalView(
                                parent_run_id=state.run_id,
                                agent=str(meta.get("agent")),
                                reason=str(meta.get("reason")),
                            )
                        )
                    else:
                        reports[child_run_id(state.run_id, invocation.invocation_id)] = meta
        children: dict[UUID, list[Mapping[str, Any]]] = {}
        tops: list[Mapping[str, Any]] = []
        for r in visible:
            if r["parent_id"] is not None and r["parent_id"] in states:
                children.setdefault(r["parent_id"], []).append(r)
            else:  # the root, or a run whose parent the caller cannot see
                tops.append(r)
        ordered: list[Mapping[str, Any]] = []

        def position(r: Mapping[str, Any]) -> tuple[int, int]:
            """Siblings in the order their parent called for them (turn, then call index)."""
            lineage = states[r["run_id"]].lineage
            numbers = re.findall(
                r"/turn/(\d+)/call/(\d+)$", lineage.parent_invocation_id if lineage else ""
            )
            return (int(numbers[0][0]), int(numbers[0][1])) if numbers else (0, 0)

        def walk(r: Mapping[str, Any]) -> None:
            ordered.append(r)
            for child in sorted(children.get(r["run_id"], []), key=position):
                walk(child)

        for top in tops:
            walk(top)
        nodes = [
            self._node(r, states[r["run_id"]], summaries, reports.get(r["run_id"])) for r in ordered
        ]
        items = [
            (node.run.objective, node.run.run_id, node.report.answer)
            for node in nodes
            if node.run.objective and node.report is not None and node.report.answer
        ]
        found = disagreements([(o, r, a) for o, r, a in items if o and a])
        root_state = states.get(root_id)
        return OrchestrationView(
            root_run_id=root_id,
            focus_run_id=focus,
            limits=root_state.delegation if root_state is not None else None,
            nodes=tuple(nodes),
            totals=self._totals(nodes, states, root_id),
            refusals=tuple(refusals),
            disagreements=tuple(found),
            hidden=len(rows) - len(visible),
        )

    def _node(
        self,
        row: Mapping[str, Any],
        state: RunState,
        summaries: Mapping[UUID, RunSummary],
        meta: Mapping[str, Any] | None,
    ) -> OrchestrationNode:
        summary = summaries[row["run_id"]]
        waiting = None
        if state.awaiting_approval:
            waiting = "approval of " + ", ".join(i.call.name for i in state.awaiting_approval)
        elif summary.phase is RunPhase.INTERRUPTED:
            waiting = "interrupted: resume the run to continue"
        elif not state.status.is_terminal and any(
            summaries[c].phase in (RunPhase.RUNNING, RunPhase.WAITING_APPROVAL)
            for c in summaries
            if c != row["run_id"] and summaries[c].parent_run_id == row["run_id"]
        ):
            waiting = "delegated work"
        report = None
        if meta is not None:
            verification = meta.get("verification")
            report = ReportView(
                outcome=str(meta.get("outcome")),
                reason=meta.get("reason"),
                status=meta.get("status"),
                confidence=meta.get("confidence"),
                answer=meta.get("answer"),
                open_questions=tuple(meta.get("open_questions") or ()),
                verification=VerificationView.model_validate(verification)
                if isinstance(verification, dict)
                else None,
                side_effects_unknown=tuple(meta.get("side_effects_unknown") or ()),
                evidence=tuple(meta.get("evidence") or ()),
            )
        return OrchestrationNode(
            run=summary,
            parent_invocation_id=state.lineage.parent_invocation_id if state.lineage else None,
            inputs=tuple(i.run_id for i in state.assignment.inputs) if state.assignment else (),
            started_at=state.created_at,
            finished_at=row["updated_at"] if state.status.is_terminal else None,
            report=report,
            narrowed=tuple(row["data"].get("narrowed") or ()),
            waiting_on=waiting,
        )

    def _totals(
        self, nodes: Sequence[OrchestrationNode], states: Mapping[UUID, RunState], root: UUID
    ) -> OrchestrationTotals:
        phases: dict[str, int] = {}
        for node in nodes:
            phases[node.run.phase.value] = phases.get(node.run.phase.value, 0) + 1
        now = _now()
        coordinators = {n.run.parent_run_id for n in nodes if n.run.parent_run_id is not None}
        coordinator_tokens = sum(
            states[n.run.run_id].total_tokens for n in nodes if n.run.run_id in coordinators
        )
        total_in = sum(n.run.usage.input_tokens for n in nodes)
        total_out = sum(n.run.usage.output_tokens for n in nodes)
        root_node = next((n for n in nodes if n.run.run_id == root), None)
        wall = (
            ((root_node.finished_at or now) - root_node.started_at).total_seconds()
            if root_node is not None
            else 0.0
        )
        agent_s = sum(
            ((n.finished_at or now) - n.started_at).total_seconds()
            for n in nodes
            if n.run.run_id != root
        )
        return OrchestrationTotals(
            runs=len(nodes),
            phases=phases,
            input_tokens=total_in,
            output_tokens=total_out,
            coordinator_tokens=coordinator_tokens,
            worker_tokens=total_in + total_out - coordinator_tokens,
            wall_s=round(max(wall, 0.0), 3),
            agent_s=round(max(agent_s, 0.0), 3),
            max_depth=max((n.run.depth for n in nodes), default=0),
            waiting_approvals=sum(n.run.awaiting_approvals for n in nodes),
            failed=sum(1 for n in nodes if n.run.phase is RunPhase.FAILED),
        )
