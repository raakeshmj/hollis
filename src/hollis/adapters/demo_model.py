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
- as a specialist (a `submit_<contract>` tool is offered, ADR-0034): a short, fixed procedure for
  its contract, then a submission built only from what the tools returned, then an answer that
  says whether Hollis accepted it. The results are honest and minimal: a code review with no
  findings (rules cannot judge code), a change only for `replace "a" with "b" in <path>`, a
  research brief quoting the top passages, a data analysis that counts a table's rows, a docs
  change only for `write <path>.md: <text>`, an automation log of one configured command;
- otherwise: an explanation that this is the demo model and how to configure a real one.

Every response starts with "[demo]" so it can never be mistaken for model output.
"""

import json
import re
from typing import cast

from pydantic import JsonValue

from hollis.runtime.context import estimate_text
from hollis.runtime.conversation import (
    AssistantTurn,
    ContextKind,
    ToolCall,
    ToolResultMessage,
    ToolResultsTurn,
    ToolSpec,
)
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
        submit = _submit_tool(tools)
        if submit is not None and (last is None or isinstance(last, ToolResultsTurn)):
            return self._specialist(request, submit)
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
            submit = _submit_tool({spec.name for spec in request.tools})
            if submit is not None:
                return self._specialist(request, submit)
            return self._answer(request, _team_summary(request))
        names = ", ".join(c.name.removeprefix("delegate_") for c in calls)
        return ModelResponse(
            content=f"[demo] Delegating to {names}.",
            stop_reason=StopReason.TOOL_USE,
            tool_calls=calls,
            usage=self._usage(request, names),
            served_model=self._descriptor.model,
        )

    def _specialist(self, request: ModelRequest, submit: str) -> ModelResponse:
        """One step of a specialist's fixed demo procedure (see the module docstring)."""
        done = _done(request.history)
        submitted = [result for call, result in done if call.name == submit]
        if submitted:
            verdict = submitted[-1]
            if verdict.is_error:
                problem = next(
                    (line[2:] for line in verdict.content.splitlines() if line.startswith("- ")),
                    _clip(verdict.content, 200),
                )
                return self._answer(
                    request, f"[demo] Hollis rejected the submission: {_clip(problem, 300)}"
                )
            return self._answer(
                request,
                f"[demo] Submitted the result with {submit}; Hollis's checks against this run "
                "accepted it. The demo model follows fixed rules, so the result is minimal.",
            )
        step = _SPECIALIST_STEPS.get(submit, _no_step)(request, done)
        if isinstance(step, ToolCall):
            return self._calls(request, (step,))
        if step is None:
            return self._answer(
                request,
                "[demo] The tools gave nothing the demo model can report under this agent's "
                "contract, so it submits nothing.",
            )
        call = ToolCall(call_id=_call_id(request, submit), name=submit, arguments=step)
        return self._calls(request, (call,))

    def _calls(self, request: ModelRequest, calls: tuple[ToolCall, ...]) -> ModelResponse:
        names = ", ".join(c.name for c in calls)
        return ModelResponse(
            content=f"[demo] Calling {names}.",
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
            text = _clip(_findings(text), 300) if text else meta.get("reason")
            lines.append(f"- {meta.get('agent')} ({outcome}): {text}")
    return "\n".join(lines)


def _review(task: str, reports: list[tuple[str, str, str]]) -> str:
    """The reports side by side, citations kept. Rules cannot judge whether they agree."""
    handles = set(tool_evidence(task))
    agents = ", ".join(agent for agent, _, _ in reports)
    lines = [f"[demo] Reviewed {len(reports)} reports ({agents}):"]
    for agent, _, body in reports:
        lines.append(f"- {agent}: {_clip(_findings(restore_citations(body, handles)), 240)}")
    return "\n".join(lines)


def _findings(body: str) -> str:
    """A demo answer's findings, without its "[demo] From the ...:" header."""
    lines = [line.strip() for line in body.removeprefix("[demo]").splitlines()]
    kept = [line.removeprefix("- ") for line in lines if line and not line.endswith(":")]
    return " ".join(kept) or body.strip()


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


# --- specialists (ADR-0034) ----------------------------------------------------------------------
#
# Each step function looks at what was done so far and returns the next tool call, the
# submission's arguments (a dict) when it is time to submit, or None when there is nothing
# honest to submit.

type Done = list[tuple[ToolCall, ToolResultMessage]]
type Step = ToolCall | dict[str, JsonValue] | None
_BUILTIN = re.compile(
    r"^(fs|code|data|docs|git|knowledge|memory|artifact|skill|submit|delegate|mcp)_|^apply_patch$"
)
_PATH = re.compile(r"(?<![\w/])((?:[\w.-]+/)*[\w-]+\.[A-Za-z0-9]{1,8})\b")
_NUMBERED = re.compile(r"^\s*(\d+) \| (.*)$", re.M)


def _submit_tool(tools: set[str]) -> str | None:
    return next((t for t in sorted(tools) if t.startswith("submit_")), None)


def _done(history: tuple[object, ...] | list[object]) -> Done:
    calls = {
        call.call_id: call
        for item in history
        if isinstance(item, AssistantTurn)
        for call in item.tool_calls
    }
    return [
        (calls[result.call_id], result)
        for item in history
        if isinstance(item, ToolResultsTurn)
        for result in item.results
        if result.call_id in calls
    ]


def _call_id(request: ModelRequest, name: str) -> str:
    """Unique within a run: the turn number and the tool."""
    turns = sum(1 for item in request.history if isinstance(item, AssistantTurn))
    return f"demo_{turns}_{name}"


def _make(request: ModelRequest, name: str, **arguments: JsonValue) -> ToolCall | None:
    if name not in {spec.name for spec in request.tools}:
        return None
    return ToolCall(call_id=_call_id(request, name), name=name, arguments=dict(arguments))


def _last(done: Done, name: str) -> tuple[ToolCall, ToolResultMessage] | None:
    return next(((c, r) for c, r in reversed(done) if c.name == name), None)


def _check_tool(request: ModelRequest) -> ToolSpec | None:
    """A configured command that looks like a check and needs no arguments."""
    for spec in request.tools:
        required = spec.input_schema.get("required") or []
        if (
            not _BUILTIN.match(spec.name)
            and not required
            and re.search(r"test|check|lint|build", spec.name)
        ):
            return spec
    return None


def _command(request: ModelRequest) -> ToolSpec | None:
    """The first configured command that needs no arguments."""
    for spec in request.tools:
        if not _BUILTIN.match(spec.name) and not (spec.input_schema.get("required") or []):
            return spec
    return None


def _no_step(request: ModelRequest, done: Done) -> Step:
    return None


def _code_review(request: ModelRequest, done: Done) -> Step:
    path = _PATH.search(request.input)
    if not done:
        if path and (call := _make(request, "code_read", path=path.group(1), start=1, end=80)):
            return call
        call = _make(request, "git_diff") or _make(
            request, "code_search", query=_keyword(request.input)
        )
        if call is not None:
            return call
    read = [c for c, r in done if not r.is_error]
    scope: list[JsonValue] = [
        str((c.arguments or {})["path"])
        for c in read
        if c.name == "code_read" and "path" in (c.arguments or {})
    ]
    looked = ", ".join(str(s) for s in scope) or "the diff and search results"
    return {
        "summary": f"[demo] Rules cannot judge code, so this review reports no findings. It "
        f"looked at {looked}.",
        "scope": scope,
        "findings": [],
    }


def _change(request: ModelRequest, done: Done) -> Step:
    rule = re.search(r'replace\s+"([^"]+)"\s+with\s+"([^"]*)"\s+in\s+(\S+)', request.input, re.I)
    if rule is None:
        return {
            "summary": '[demo] The demo model only makes changes asked as: replace "a" with "b" '
            "in <path>. Nothing was changed.",
        }
    old, new, path = rule.group(1), rule.group(2), rule.group(3).rstrip(".")
    read = _last(done, "code_read")
    if read is None:
        return _make(request, "code_read", path=path, start=1, end=400) or {
            "summary": "[demo] No code tools: nothing was changed."
        }
    patched = _last(done, "apply_patch")
    if patched is None and not read[1].is_error:
        lines = {int(n): text for n, text in _NUMBERED.findall(read[1].content)}
        target = next((n for n in sorted(lines) if old in lines[n]), None)
        if target is not None:
            patch = _patch(path, lines, target, lines[target].replace(old, new, 1))
            if call := _make(request, "apply_patch", patch=patch):
                return call
    check = _check_tool(request)
    applied = patched is not None and not patched[1].is_error
    verified = next(((c, r) for c, r in done if check and c.name == check.name), None)
    if applied and check is not None and verified is None:
        return ToolCall(call_id=_call_id(request, check.name), name=check.name, arguments={})
    changes: list[JsonValue] = (
        [{"file": path, "summary": f"replaced {old!r} with {new!r}"}] if applied else []
    )
    verifications: list[JsonValue] = (
        [
            {
                "call_id": verified[0].call_id,
                "command": verified[0].name,
                "outcome": "failed" if verified[1].is_error else "passed",
            }
        ]
        if verified
        else []
    )
    if applied:
        summary = f'[demo] Replaced "{old}" with "{new}" in {path}.'
    elif patched is not None:
        summary = f"[demo] The patch to {path} was not applied: {_clip(patched[1].content, 160)}"
    else:
        summary = f'[demo] "{old}" was not found in {path}; nothing was changed.'
    return {"summary": summary, "changes": changes, "verifications": verifications}


def _patch(path: str, lines: dict[int, str], target: int, replacement: str) -> str:
    before = [n for n in (target - 1,) if n in lines]
    after = [n for n in (target + 1,) if n in lines]
    start = before[0] if before else target
    count = len(before) + 1 + len(after)
    body = [f" {lines[n]}" for n in before] + [f"-{lines[target]}", f"+{replacement}"]
    body += [f" {lines[n]}" for n in after]
    header = f"--- a/{path}\n+++ b/{path}\n@@ -{start},{count} +{start},{count} @@\n"
    return header + "\n".join(body) + "\n"


def _research(request: ModelRequest, done: Done) -> Step:
    reports = [(c, r) for c, r in done if c.name.startswith("delegate_")]
    if reports:  # a coordinator: cite the completed delegations by run id
        findings: list[JsonValue] = []
        for _, result in reports:
            meta = report_meta(result.content)
            if meta is None or meta.get("outcome") != "completed":
                continue
            output = meta.get("output")
            status = (
                cast(dict[str, JsonValue], output).get("status")
                if isinstance(output, dict)
                else None
            )
            if isinstance(output, dict) and status != "accepted":
                continue  # its answer was not checked: not a source
            bodies = " ".join(b for _, _, b in extract_reports(result.content))
            text = _clip(_findings(bodies), 240) if bodies else "a result"
            findings.append(
                {
                    "statement": f"{meta.get('agent')} reported: {text}",
                    "citations": [str(meta["run_id"])],
                }
            )
        if not findings:
            return None
        return {
            "question": _clip(request.input, 300),
            "summary": "[demo] The specialists' reports, side by side. Rules cannot judge "
            "whether they agree.",
            "findings": findings,
        }
    search = _last(done, "knowledge_search")
    if search is None:
        return _make(request, "knowledge_search", query=request.input.strip()[:200])
    blocks = [] if search[1].is_error else _BLOCK.findall(search[1].content)
    if not blocks:
        return None
    return {
        "question": _clip(request.input, 300),
        "summary": "[demo] The top passages, quoted. The demo model does not weigh or combine "
        "sources.",
        "findings": [
            {"statement": _first_sentence(body), "citations": [handle]}
            for handle, body in blocks[:3]
        ],
        "gaps": ["The demo model quotes passages; it does not check them against each other."],
    }


def _analysis(request: ModelRequest, done: Done) -> Step:
    listing = _last(done, "data_list")
    if listing is None:
        return _make(request, "data_list", path=".")
    first = re.search(r"^(\S+)\t.*\ttable (\w+)$", listing[1].content, re.M)
    if first is None:
        return None
    path, table = first.group(1), first.group(2)
    query = _last(done, "data_query")
    if query is None:
        sql = f'SELECT COUNT(*) AS row_count FROM "{table}"'  # noqa: S608  # a listed table name, quoted
        return _make(request, "data_query", sql=sql, files=[path])
    qid = re.search(r"^query_id: (Q-[0-9a-f]{8})$", query[1].content, re.M)
    count = re.search(r"^\| (\d+) \|$", query[1].content, re.M)
    if query[1].is_error or qid is None or count is None:
        return None
    return {
        "question": _clip(request.input, 300),
        "answer": f"[demo] {path} has {count.group(1)} rows. The demo model only counts rows.",
        "findings": [
            {
                "statement": f"{path} has {count.group(1)} rows",
                "value": int(count.group(1)),
                "query_id": qid.group(1),
            }
        ],
        "caveats": ["The demo model does not analyse data; it counts the rows of one table."],
    }


def _docs(request: ModelRequest, done: Done) -> Step:
    rule = re.match(
        r"(?:please\s+)?write\s+(\S+\.md)\s*:\s*(.+)$", request.input.strip(), re.I | re.S
    )
    if rule is None:
        return None
    path = rule.group(1)
    written = _last(done, "fs_write_file")
    if written is None:
        return _make(request, "fs_write_file", path=path, content=rule.group(2).strip() + "\n")
    if written[1].is_error:
        return None
    if _last(done, "docs_check") is None:
        return _make(request, "docs_check", path=path)
    return {
        "summary": f"[demo] Wrote {path} as asked.",
        "files": [{"file": path, "summary": "written as asked"}],
    }


def _automation(request: ModelRequest, done: Done) -> Step:
    plan: list[JsonValue] = [
        _clip(part, 200)
        for part in re.split(r"\bthen\b|;", request.input, flags=re.I)
        if part.strip()
    ] or [_clip(request.input, 200)]
    command = _command(request)
    ran = [(c, r) for c, r in done if command is not None and c.name == command.name]
    if command is not None and not ran:
        return ToolCall(call_id=_call_id(request, command.name), name=command.name, arguments={})
    steps: list[JsonValue] = []
    unknown: list[JsonValue] = []
    for call, result in ran:
        outcome = _step_outcome(result)
        steps.append(
            {"description": f"run {call.name}", "call_id": call.call_id, "outcome": outcome}
        )
        if outcome == "unknown":
            unknown.append(call.call_id)
    summary = (
        f"[demo] Ran {command.name} once; the demo model does not plan further steps."
        if command is not None
        else "[demo] No configured command needs no arguments, so nothing was run."
    )
    return {"summary": summary, "plan": plan, "steps": steps, "unknown_side_effects": unknown}


def _step_outcome(result: ToolResultMessage) -> str:
    if not result.is_error:
        return "succeeded"
    text = result.content.casefold()
    if text.startswith(("tool call rejected", "tool call denied", "tool call not approved")):
        return "not_run"
    if "unknown" in text or "ambiguous" in text:
        return "unknown"
    return "failed"


def _keyword(text: str) -> str:
    words = re.findall(r"[A-Za-z_]{4,}", text)
    return words[-1] if words else text.strip()[:40] or "TODO"


_SPECIALIST_STEPS = {
    "submit_code_review": _code_review,
    "submit_change_report": _change,
    "submit_research_brief": _research,
    "submit_data_analysis": _analysis,
    "submit_docs_change": _docs,
    "submit_automation_log": _automation,
}
