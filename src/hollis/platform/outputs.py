"""What a run of an agent is given, and its structured output (ADR-0033 to ADR-0035).

`run_setup` assembles a run from an agent: its tools, plus the `submit_<contract>` tool when it
has an output contract and `skill_read` when it is offered skills; its instructions, followed by
the skills it is offered (name and description only) and the contract's rules; and its policy
overlay rules after every rule of the run that delegated it, so a delegated run is always held
to at least what its parent was.

`SubmitTool` is the contract at run time. Its arguments are validated by the contract's schema
(errors go back to the model as an invalid call), then checked against the run's own log;
problems go back as an error result, and the model can fix them and submit again within its
budget. An accepted submission records the output as a JSON part: the log stays the only
source of truth, and `output_view` derives the run's output from it.
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from pydantic import JsonValue, ValidationError

from hollis.platform.schemas import Agent, OutputView
from hollis.runtime.events import ToolOutcome
from hollis.runtime.grounding import neutralize
from hollis.runtime.results import render_parts
from hollis.runtime.state import Invocation, RunState
from hollis.runtime.store import load_run
from hollis.runtime.tools import (
    JsonPart,
    PolicyOverlayRule,
    TextPart,
    ToolArgumentError,
    ToolContext,
    ToolDescriptor,
    ToolEffect,
    ToolOutput,
    ToolTrust,
)
from hollis.specialists import CONTRACTS, OUTPUT_KIND, Contract, RunFacts, load_skills, offer
from hollis.specialists.contracts import SUBMIT_PREFIX

if TYPE_CHECKING:
    from hollis.platform.workspace import Workspace

SKILL_TOOL = "skill_read"
_TOOL_CONTRACTS = {c.tool_name: c for c in CONTRACTS.values()}


@dataclass(frozen=True)
class RunSetup:
    instructions: str
    tools: tuple[str, ...]
    policy: tuple[PolicyOverlayRule, ...]


def run_setup(
    ws: "Workspace", agent: Agent, *, inherited: Sequence[PolicyOverlayRule] = ()
) -> RunSetup:
    tools = list(agent.tools)
    sections = [agent.instructions.strip()]
    if agent.skills and ws.registry.get(SKILL_TOOL) is not None:
        found, _ = load_skills(ws.skills_dir)
        offered = [s for s in found if s.name in agent.skills]
        if offered:
            sections.append(offer(offered))
            tools.append(SKILL_TOOL)
    contract = CONTRACTS.get(agent.contract) if agent.contract else None
    if contract is not None:
        sections.append(contract.guidance())
        tools.append(contract.tool_name)
    return RunSetup(
        instructions="\n\n".join(s for s in sections if s),
        tools=tuple(dict.fromkeys(tools)),
        policy=(*inherited, *agent.policy),
    )


class SubmitTool:
    """`submit_<contract>`: submit the run's result under its output contract."""

    def __init__(self, ws: "Workspace", contract: Contract) -> None:
        self._ws = ws
        self._contract = contract
        self._descriptor = ToolDescriptor(
            name=contract.tool_name,
            version=contract.version,
            description=(
                f"Submit your result as a {contract.name.lower()}: {contract.description} "
                "Hollis checks it against what this run actually did and either accepts it as "
                "the run's output or lists the problems for you to fix and submit again."
            ),
            input_schema=cast(dict[str, JsonValue], contract.schema.model_json_schema()),
            effect=ToolEffect.READ_ONLY,
            idempotent=True,
            trust=ToolTrust.TRUSTED,  # the result is Hollis's verdict; echoed values are framed
            timeout_s=30,
        )

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._descriptor

    def validate(self, arguments: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        try:
            parsed = self._contract.schema.model_validate(dict(arguments))
        except ValidationError as exc:
            problems = [
                f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}"
                for e in exc.errors(include_input=False, include_url=False)[:10]
            ]
            raise ToolArgumentError("; ".join(problems)) from None
        return cast(dict[str, JsonValue], parsed.model_dump(mode="json"))

    async def invoke(self, arguments: dict[str, JsonValue], context: ToolContext) -> ToolOutput:
        output = self._contract.schema.model_validate(arguments)
        state = await load_run(self._ws.runs, context.run_id)
        facts = RunFacts.of(state, self._ws.fs_root)
        problems = await asyncio.to_thread(self._contract.check, output, facts)
        if problems:
            listed = "\n".join(f"- {neutralize(p)}" for p in problems[:30])
            more = f"\n- ... and {len(problems) - 30} more" if len(problems) > 30 else ""
            return ToolOutput.text(
                f"Rejected: the {self._contract.name.lower()} does not match what this run did."
                f"\n{listed}{more}\nFix these (do the missing work, or correct the report) and "
                "submit again.",
                is_error=True,
            )
        record: dict[str, JsonValue] = {
            "kind": OUTPUT_KIND,
            "contract": self._contract.id,
            "version": self._contract.version,
            "output": cast(JsonValue, output.model_dump(mode="json")),
        }
        return ToolOutput(
            parts=(
                JsonPart(data=record),
                TextPart(
                    text=f"Accepted: this {self._contract.name.lower()} is the run's output. "
                    "End with a short summary for the person who asked."
                ),
            )
        )

    async def reconcile(self, context: ToolContext) -> ToolOutput | None:
        return None


def _record(invocation: Invocation) -> dict[str, Any] | None:
    for part in invocation.parts:
        if isinstance(part, JsonPart) and isinstance(part.data, dict):
            data = cast(dict[str, Any], part.data)
            if data.get("kind") == OUTPUT_KIND:
                return data
    return None


def _problems(invocation: Invocation) -> tuple[str, ...]:
    if invocation.outcome is ToolOutcome.INVALID:
        return (invocation.reason or "invalid arguments",)
    lines = render_parts(invocation.parts).splitlines()
    listed = tuple(line[2:] for line in lines if line.startswith("- "))
    return listed or ((lines[0],) if lines else ())


def output_view(state: RunState) -> OutputView | None:
    """The run's structured output, from its log: the latest accepted submission, else the
    latest rejection's problems, else missing. None when the run has no contract."""
    tool = next((t.name for t in state.tools if t.name in _TOOL_CONTRACTS), None)
    if tool is None:
        return None
    submissions = [
        inv
        for turn in state.turns
        for inv in turn.invocations
        if inv.call.name.startswith(SUBMIT_PREFIX) and inv.finished
    ]
    contract = _TOOL_CONTRACTS[tool].id
    for invocation in reversed(submissions):
        record = _record(invocation) if invocation.outcome is ToolOutcome.SUCCESS else None
        if record is not None:
            return OutputView(
                contract=contract,
                status="accepted",
                attempts=len(submissions),
                data=cast(dict[str, JsonValue], record.get("output")),
                invocation_id=invocation.invocation_id,
            )
    if submissions:
        last = submissions[-1]
        return OutputView(
            contract=contract,
            status="rejected",
            attempts=len(submissions),
            invocation_id=last.invocation_id,
            problems=_problems(last),
        )
    return OutputView(contract=contract, status="missing", attempts=0)
