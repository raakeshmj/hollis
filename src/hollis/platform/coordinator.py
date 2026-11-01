"""The deterministic `workflow` coordinator: runs a fixed delegation plan (ADR-0030).

A plan run is an ordinary run whose "model" is this coordinator: deterministic code, not a
language model, with no tokens and no network. Each turn it reads the plan from the run input and
the delegation reports in the history, then either delegates the next wave of ready steps (in
one turn, so independent read-only steps run concurrently) or writes the final answer:

- a step is ready when every step it depends on completed; their reports are passed to it with
  `inputs`, verbatim;
- a step whose dependency did not complete is skipped, and the answer says so;
- steps asked the same objective have their self-reported answers compared, and disagreements
  are reported, not resolved;
- `answer="summary"` reports every step; `answer="last"` hands off to the one final step.

Because the plan and the reports are in the run's own log, the coordinator's decisions are
reproducible: a resumed run makes the same calls.
"""

import json
import re
from uuid import UUID

from pydantic import ValidationError

from hollis.platform.orchestration import delegate_tool_name, disagreements
from hollis.platform.schemas import Plan, PlanStep
from hollis.runtime.conversation import AssistantTurn, ToolCall, ToolResultMessage
from hollis.runtime.delegation import extract_reports, report_meta, restore_citations
from hollis.runtime.grounding import tool_evidence
from hollis.runtime.model import (
    DeploymentDescriptor,
    Locality,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    StopReason,
    TextCapabilities,
    Usage,
    UsageReporting,
)

WORKFLOW_DEPLOYMENT = "workflow"
PROTOCOL = "hollis-workflow"
MODEL = "hollis-workflow-1"
_PLAN = re.compile(r"```hollis-plan\n(.*?)\n```", re.S)


def plan_input(objective: str, plan: Plan) -> str:
    """The run input of a plan run: the objective, then the plan the coordinator follows."""
    return f"{objective}\n\n```hollis-plan\n{plan.model_dump_json(indent=2)}\n```"


def read_plan(text: str) -> Plan | None:
    """The plan `plan_input` appended: the last plan block (the objective cannot hold one)."""
    matches = list(_PLAN.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    try:
        return Plan.model_validate_json(match.group(1))
    except ValidationError:
        return None


def workflow_descriptor() -> DeploymentDescriptor:
    return DeploymentDescriptor(
        deployment=WORKFLOW_DEPLOYMENT,
        provider="hollis",
        protocol=PROTOCOL,
        model=MODEL,
        locality=Locality.LOCAL,
        deduplicates_requests=True,  # deterministic: the same request gives the same response
        capabilities=ModelCapabilities(
            text=TextCapabilities(
                tools=True,
                # Large enough that a plan's history is never compacted: the coordinator reads
                # every report from it.
                context_window_tokens=10_000_000,
                usage_reporting=UsageReporting.EXACT,
            )
        ),
    )


class _Result:
    def __init__(self, step: PlanStep, message: ToolResultMessage) -> None:
        self.step = step
        self.meta = report_meta(message.content) or {}
        self.outcome = str(self.meta.get("outcome", "failed"))
        self.ok = self.outcome == "completed"
        handles = set(tool_evidence(message.content))
        bodies = [
            restore_citations(body, handles) for _, _, body in extract_reports(message.content)
        ]
        self.body = "\n".join(bodies).strip()
        self.run_id = str(self.meta.get("run_id", ""))


class WorkflowCoordinator:
    def __init__(self) -> None:
        self._descriptor = workflow_descriptor()

    @property
    def descriptor(self) -> DeploymentDescriptor:
        return self._descriptor

    async def generate(self, request: ModelRequest) -> ModelResponse:
        plan = read_plan(request.input)
        if plan is None:
            return self._answer("The workflow coordinator found no valid plan in the run input.")
        issued: set[str] = set()
        messages: dict[str, ToolResultMessage] = {}
        for item in request.history:
            if isinstance(item, AssistantTurn):
                issued.update(call.call_id for call in item.tool_calls)
            else:
                messages.update({m.call_id: m for m in item.results})
        steps = {step.id: step for step in plan.steps}
        results = {sid: _Result(steps[sid], m) for sid, m in messages.items() if sid in steps}
        offered = {spec.name for spec in request.tools}
        ready = [
            step
            for step in plan.steps
            if step.id not in issued
            and all(d in results and results[d].ok for d in step.depends_on)
            and delegate_tool_name(step.agent) in offered
        ]
        if ready:
            return ModelResponse(
                content="Delegating " + ", ".join(f"{s.id} to {s.agent}" for s in ready) + ".",
                stop_reason=StopReason.TOOL_USE,
                tool_calls=tuple(self._call(step) for step in ready),
                usage=Usage(input_tokens=0, output_tokens=0),
                served_model=MODEL,
            )
        return self._answer(self._final(plan, results))

    def _call(self, step: PlanStep) -> ToolCall:
        arguments: dict[str, object] = {"objective": step.objective}
        if step.context:
            arguments["context"] = step.context
        if step.expected_output:
            arguments["expected_output"] = step.expected_output
        if step.depends_on:
            arguments["inputs"] = list(step.depends_on)  # call ids are step ids
        return ToolCall(
            call_id=step.id,
            name=delegate_tool_name(step.agent),
            arguments=json.loads(json.dumps(arguments)),
        )

    def _final(self, plan: Plan, results: dict[str, _Result]) -> str:
        if plan.answer == "last":
            [sink] = plan.sinks
            result = results.get(sink)
            if result is not None and result.ok and result.body:
                step = result.step
                return f"{result.body}\n\n(Handed off to `{step.agent}`, step {step.id}.)"
        completed = sum(1 for r in results.values() if r.ok)
        lines = [
            f"Plan finished: {completed} of {len(plan.steps)} steps completed"
            + (f", {len(results) - completed} did not" if len(results) > completed else "")
            + (
                f", {len(plan.steps) - len(results)} skipped"
                if len(plan.steps) > len(results)
                else ""
            )
            + "."
        ]
        for step in plan.steps:
            result = results.get(step.id)
            if result is None:
                blocked = [d for d in step.depends_on if not (d in results and results[d].ok)]
                lines += ["", f"## {step.id} ({step.agent}): skipped"]
                lines.append(
                    f"Not run: it depends on {', '.join(blocked)}, which did not complete."
                )
                continue
            lines += ["", f"## {step.id} ({step.agent}): {result.outcome}"]
            if result.ok:
                lines.append(result.body or "(no answer)")
            else:
                lines.append(str(result.meta.get("reason") or "No reason was recorded."))
        found = disagreements(
            [
                (r.step.objective, UUID(r.run_id), str(r.meta["answer"]))
                for r in results.values()
                if r.ok and r.meta.get("answer") and r.run_id
            ]
        )
        if found:
            lines += ["", "Disagreements (kept as reported, not resolved):"]
            names = {r.run_id: r.step.id for r in results.values()}
            for item in found:
                answers = "; ".join(
                    f"{names.get(str(run), run)}: {answer}"
                    for run, answer in zip(item.runs, item.answers, strict=True)
                )
                lines.append(f"- {item.objective}: {answers}")
        return "\n".join(lines)

    def _answer(self, text: str) -> ModelResponse:
        return ModelResponse(
            content=text,
            stop_reason=StopReason.END_TURN,
            usage=Usage(input_tokens=0, output_tokens=0),
            served_model=MODEL,
        )
