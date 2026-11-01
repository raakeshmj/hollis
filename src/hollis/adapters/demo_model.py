"""The built-in demo model: deterministic, rule-based, and clearly not a language model.

It lets a fresh workspace exercise the whole platform with no credentials and no network: tool
calls (and therefore policy and approvals), knowledge retrieval with citations, memory, and
multi-turn runs. Behaviour is a small set of rules applied to the request:

- first turn, with tools offered: one tool call when the task asks for it, for example
  "search <query>" or a question (knowledge_search), "list files" (fs_list_dir),
  "read <path>" (fs_read_file), "write <path>: <text>" (fs_write_file, which needs approval),
  "remember that <fact>" (memory_remember), "what do you remember about <x>" (memory_recall);
- after tool results: an answer that reports them, citing knowledge handles it saw;
- with knowledge passages in context and no tool needed: an answer quoting the top passages
  with their citation keys;
- with `delegate_*` tools offered (an agent with delegates, ADR-0030): tasks that mention agents
  are delegated: "@researcher find X and @analyst check Y" delegates both at once (they run
  concurrently when read-only); "... then @reviewer compare them" delegates in a later turn and
  passes the earlier reports along; after the last wave, a summary of every report;
- as a delegated agent: earlier reports in its input are reviewed side by side, and every answer
  ends with the JSON self-report the report protocol asks for;
- otherwise: an explanation that this is the demo model and how to configure a real one.

Every response starts with "[demo]" so it can never be mistaken for model output.
"""

import json
import re

from pydantic import JsonValue

from hollis.runtime.context import estimate_text
from hollis.runtime.conversation import AssistantTurn, ContextKind, ToolCall, ToolResultsTurn
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

PROTOCOL = "hollis-demo"
DEMO_DEPLOYMENT = "demo"
REPORT_PROTOCOL_MARKER = "Report protocol (Hollis)"
_MENTION = re.compile(r"@([a-z0-9][a-z0-9-]{0,62})\b")

# A knowledge tool result is a list of blocks, each starting a line with its citation handle.
_BLOCK = re.compile(
    r"^\[(C-[0-9a-f]{8})\][^\n]*\n(.*?)(?=^\[C-[0-9a-f]{8}\]|^</knowledge-results>|\Z)",
    re.M | re.S,
)


def demo_descriptor(deployment: str = DEMO_DEPLOYMENT) -> DeploymentDescriptor:
    return DeploymentDescriptor(
        deployment=deployment,
        provider="hollis",
        protocol=PROTOCOL,
        model="hollis-demo-rules-1",
        locality=Locality.LOCAL,
        deduplicates_requests=True,  # deterministic: repeating a request repeats the answer
        capabilities=ModelCapabilities(
            text=TextCapabilities(
                tools=True,
                context_window_tokens=32_000,
                usage_reporting=UsageReporting.APPROXIMATE,
            )
        ),
    )


class DemoModelProvider:
    def __init__(self, deployment: str = DEMO_DEPLOYMENT) -> None:
        self._descriptor = demo_descriptor(deployment)

    @property
    def descriptor(self) -> DeploymentDescriptor:
        return self._descriptor

    async def generate(self, request: ModelRequest) -> ModelResponse:
        tools = {spec.name for spec in request.tools}
        last = request.history[-1] if request.history else None
        team = _team_plan(request.input, tools)
        if team and (last is None or isinstance(last, ToolResultsTurn)):
            return self._delegate(request, team)
        if isinstance(last, ToolResultsTurn):
            text = self._report(last)
            return self._answer(request, text)
        if not request.history:
            call = self._tool_call(request.input, tools)
            if call is not None:
                return ModelResponse(
                    content=f"[demo] Calling {call.name} for this task.",
                    stop_reason=StopReason.TOOL_USE,
                    tool_calls=(call,),
                    usage=self._usage(request, call.name),
                    served_model=self._descriptor.model,
                )
        reports = extract_reports(request.input)
        if reports:
            return self._answer(request, _review(request.input, reports))
        passages = [b for b in request.context if b.kind is ContextKind.KNOWLEDGE]
        if passages:
            lines = ["[demo] From the sources:"]
            for block in passages[:3]:
                key = block.source.split(":", 1)[1]
                body = block.text.split("\n", 1)[1] if "\n" in block.text else block.text
                lines.append(f"- {_cite(_first_sentence(body), key)}")
            return self._answer(request, "\n".join(lines))
        return self._answer(
            request,
            "[demo] This is the Hollis demo model: deterministic rules, not a language model. "
            f'It received your task: "{_clip(request.input, 200)}". Add a real deployment '
            "(`hollis models add`) to get model answers; tools, approvals, memory and knowledge "
            "already work with this one.",
        )

    def _tool_call(self, task: str, tools: set[str]) -> ToolCall | None:
        text = task.strip()
        lowered = text.casefold()
        write = re.match(r"(?:please\s+)?write\s+(\S+?)\s*:\s*(.+)$", text, re.I | re.S)
        if write and "fs_write_file" in tools:
            return _call("fs_write_file", path=write.group(1), content=write.group(2).strip())
        read = re.match(r"(?:please\s+)?read\s+(\S+)", text, re.I)
        if read and "fs_read_file" in tools:
            return _call("fs_read_file", path=read.group(1).rstrip(".?!"))
        if re.search(r"\blist (the )?files\b", lowered) and "fs_list_dir" in tools:
            return _call("fs_list_dir", path=".")
        remember = re.match(r"(?:please\s+)?remember(?: that)?\s+(.+)$", text, re.I | re.S)
        if remember and "memory_remember" in tools:
            return _call("memory_remember", content=remember.group(1).strip(), kind="fact")
        recall = re.search(r"what do you remember(?: about)?\s*(.*)$", text, re.I | re.S)
        if recall and "memory_recall" in tools:
            return _call("memory_recall", query=recall.group(1).strip(" ?") or text)
        if "knowledge_search" in tools and (
            lowered.startswith(("search", "find", "look up"))
            or "?" in text
            or lowered.startswith(("what", "how", "why", "when", "where", "who", "which"))
        ):
            query = re.sub(r"^(search( for)?|find|look up)\s+", "", text, flags=re.I)
            return _call("knowledge_search", query=query.strip())
        return None

    def _report(self, results: ToolResultsTurn) -> str:
        lines = ["[demo] From the tools:"]
        for result in results.results:
            blocks = [] if result.is_error else _BLOCK.findall(result.content)
            for handle, body in blocks[:3]:
                lines.append(f"- {_cite(_first_sentence(body), handle)}")
            if not blocks:
                status = "failed" if result.is_error else "returned"
                lines.append(f"- {result.name} {status}: {_clip(_plain(result.content), 300)}")
        return "\n".join(lines)

    def _delegate(
        self, request: ModelRequest, team: list[tuple[int, int, str, str]]
    ) -> ModelResponse:
        issued = {
            call.call_id
            for item in request.history
            if isinstance(item, AssistantTurn)
            for call in item.tool_calls
        }
        done_waves = {wave for index, wave, _, _ in team if f"demo_delegate_{index}" in issued}
        wave = max(done_waves) + 1 if done_waves else 0
        calls = tuple(
            ToolCall(
                call_id=f"demo_delegate_{index}",
                name="delegate_" + agent.replace("-", "_"),
                arguments=_delegation_arguments(objective, sorted(issued)),
            )
            for index, step_wave, agent, objective in team
            if step_wave == wave
        )
        if not calls:
            return self._answer(request, _team_summary(request))
        names = ", ".join(c.name.removeprefix("delegate_") for c in calls)
        return ModelResponse(
            content=f"[demo] Delegating to {names}.",
            stop_reason=StopReason.TOOL_USE,
            tool_calls=calls,
            usage=self._usage(request, names),
            served_model=self._descriptor.model,
        )

    def _answer(self, request: ModelRequest, text: str) -> ModelResponse:
        if request.instructions and REPORT_PROTOCOL_MARKER in request.instructions:
            text = _with_self_report(text)
        return ModelResponse(
            content=text,
            stop_reason=StopReason.END_TURN,
            usage=self._usage(request, text),
            served_model=self._descriptor.model,
        )

    @staticmethod
    def _usage(request: ModelRequest, output: str) -> Usage:
        prompt = (
            request.input + (request.instructions or "") + "".join(b.text for b in request.context)
        )
        return Usage(input_tokens=estimate_text(prompt) + 8, output_tokens=estimate_text(output))


def _call(name: str, **arguments: str) -> ToolCall:
    return ToolCall(call_id=f"demo_{name}", name=name, arguments=dict(arguments))


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _plain(text: str) -> str:
    lines = [
        line
        for line in text.splitlines()
        if not line.startswith(
            ("Passages from indexed", "Recalled memories are", "<", "The content below")
        )
    ]
    return " ".join(lines)


def _cite(sentence: str, key: str) -> str:
    """The citation goes inside the sentence, before its final punctuation."""
    stripped = sentence.rstrip()
    end = stripped[-1] if stripped and stripped[-1] in ".!?" else "."
    return f"{stripped.rstrip('.!?')} [{key}]{end}"


def _first_sentence(text: str) -> str:
    flat = " ".join(text.split())
    match = re.match(r"(.{20,240}?[.!?])(\s|$)", flat)
    return match.group(1) if match else _clip(flat, 240)


def _delegation_arguments(objective: str, inputs: list[str]) -> dict[str, JsonValue]:
    arguments: dict[str, JsonValue] = {"objective": objective}
    if inputs:
        arguments["inputs"] = list[JsonValue](inputs)
    return arguments


def _team_plan(task: str, tools: set[str]) -> list[tuple[int, int, str, str]]:
    """(index, wave, agent, objective) for each @agent the task mentions that can be delegated
    to. "then" before a mention starts a new wave."""
    mentions = [
        m for m in _MENTION.finditer(task) if "delegate_" + m.group(1).replace("-", "_") in tools
    ]
    plan: list[tuple[int, int, str, str]] = []
    wave = 0
    for i, mention in enumerate(mentions):
        before = task[mentions[i - 1].end() if i else 0 : mention.start()]
        if i and re.search(r"\bthen\b", before, re.I):
            wave += 1
        end = mentions[i + 1].start() if i + 1 < len(mentions) else len(task)
        objective = re.sub(
            r"(?:[\s,;]|\b(?:and|then)\b)+$", "", task[mention.end() : end], flags=re.I
        )
        plan.append((i, wave, mention.group(1), objective.strip(" ,;:") or task.strip()))
    return plan


def _team_summary(request: ModelRequest) -> str:
    lines = ["[demo] From the team:"]
    for item in request.history:
        if not isinstance(item, ToolResultsTurn):
            continue
        for result in item.results:
            meta = report_meta(result.content)
            if meta is None:
                continue
            outcome = meta.get("outcome")
            bodies = [b for _, _, b in extract_reports(result.content)]
            text = restore_citations(" ".join(bodies), set(tool_evidence(result.content)))
            text = _clip(text.removeprefix("[demo]").strip(), 300) if text else meta.get("reason")
            lines.append(f"- {meta.get('agent')} ({outcome}): {text}")
    return "\n".join(lines)


def _review(task: str, reports: list[tuple[str, str, str]]) -> str:
    handles = set(tool_evidence(task))
    agents = ", ".join(agent for agent, _, _ in reports)
    lines = [f"[demo] Reviewed {len(reports)} reports ({agents}):"]
    for agent, _, body in reports:
        text = restore_citations(body.removeprefix("[demo]").strip(), handles)
        lines.append(f"- {agent}: {_clip(text, 240)}")
    firsts = {_first_sentence(b.removeprefix("[demo]")).casefold() for _, _, b in reports}
    lines.append(
        "They agree." if len(firsts) == 1 else "They differ; check the evidence for each claim."
    )
    return "\n".join(lines)


def _with_self_report(text: str) -> str:
    body = text.removeprefix("[demo]").strip()
    blocked = "This is the Hollis demo model" in body
    report: dict[str, object] = {
        "status": "blocked" if blocked else "done",
        "answer": _first_sentence(_plain(body.split("\n", 1)[-1] or body)),
        "confidence": "low",
        "open_questions": [],
    }
    return f"{text}\n\n```json\n{json.dumps(report)}\n```"
