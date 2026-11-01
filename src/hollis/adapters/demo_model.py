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
- otherwise: an explanation that this is the demo model and how to configure a real one.

Every response starts with "[demo]" so it can never be mistaken for model output.
"""

import re

from hollis.runtime.context import estimate_text
from hollis.runtime.conversation import ContextKind, ToolCall, ToolResultsTurn
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
        passages = [b for b in request.context if b.kind is ContextKind.KNOWLEDGE]
        if passages:
            lines = ["[demo] From the knowledge passages provided:"]
            for block in passages[:3]:
                key = block.source.split(":", 1)[1]
                body = block.text.split("\n", 1)[1] if "\n" in block.text else block.text
                lines.append(f"- {_first_sentence(body)} [{key}]")
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
        lines = ["[demo] Here is what the tools returned:"]
        for result in results.results:
            blocks = [] if result.is_error else _BLOCK.findall(result.content)
            for handle, body in blocks[:3]:
                lines.append(f"- {_first_sentence(body)} [{handle}]")
            if not blocks:
                status = "failed" if result.is_error else "returned"
                lines.append(f"- {result.name} {status}: {_clip(_plain(result.content), 300)}")
        return "\n".join(lines)

    def _answer(self, request: ModelRequest, text: str) -> ModelResponse:
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


def _first_sentence(text: str) -> str:
    flat = " ".join(text.split())
    match = re.match(r"(.{20,240}?[.!?])(\s|$)", flat)
    return match.group(1) if match else _clip(flat, 240)
