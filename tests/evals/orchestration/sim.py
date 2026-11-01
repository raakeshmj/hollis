"""Simulated agents for the orchestration evaluation: deterministic policies, not models.

They behave like a careful tool-using model would on these tasks, so the evaluation measures
what coordination changes (context per agent, turns, parallelism, failure isolation, permission
narrowing, budget enforcement, recovery) rather than model intelligence:

- `worker` answers the questions in its task: one `knowledge_search` per question and turn
  (or all at once with `parallel=True`), then one line per question quoting the best-matching
  sentence of the evidence it saw, with its citation, or "the sources do not say". Results passed
  to it from earlier steps are added to its searches.
- `lead` routes each question to the delegate whose description fits best (one delegation per
  delegate, all in the first turn), then joins the reports and names what was not answered.
- `reviewer` compares earlier reports; when they differ it prefers the one citing the most recent
  year, and says that the sources disagree.

Token usage is estimated from everything the request contains, including history, so the cost
of long single-agent contexts is visible. Each call sleeps to simulate model latency.
"""

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from hollis.runtime.context import estimate_text
from hollis.runtime.conversation import AssistantTurn, ToolCall, ToolResultsTurn
from hollis.runtime.delegation import extract_reports, report_meta, restore_citations
from hollis.runtime.grounding import tool_evidence
from hollis.runtime.model import (
    DeploymentDescriptor,
    FailureKind,
    Locality,
    ModelCallError,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    StopReason,
    TextCapabilities,
    Usage,
)
from hollis.runtime.text import query_terms

BASE_LATENCY_S = 0.02
PER_INPUT_TOKEN_S = 4e-6
PER_OUTPUT_TOKEN_S = 2e-4
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_YEAR = re.compile(r"\b(20\d\d)\b")


def _terms(text: str) -> set[str]:
    """Content words, crudely stemmed (five-letter prefixes: deploy ~ deployed)."""
    return {t[:5] for t in query_terms(text) if len(t) > 2}


def _sentences(text: str) -> list[str]:
    lines = [line for line in text.splitlines() if line and not line.startswith(("(", "<", "#"))]
    return [s.strip() for line in lines for s in _SENTENCE.split(line) if s.strip()]


def best_sentence(
    question: str, evidence: dict[str, str], hint: str = ""
) -> tuple[str, str] | None:
    """The evidence sentence sharing the most content words with the question; `hint` (what
    is already known) only breaks ties. None when nothing shares two words."""
    wanted, hinted = _terms(question), _terms(hint)
    best: tuple[int, int, str, str] | None = None
    for handle, text in evidence.items():
        for sentence in _sentences(text):
            words = _terms(sentence)
            score, tie = len(wanted & words), len(hinted & words)
            if score >= 2 and (best is None or (score, tie) > (best[0], best[1])):
                best = (score, tie, sentence, handle)
    return (best[2], best[3]) if best else None


def questions_in(task: str) -> list[str]:
    """Bullet questions in the task's first section (before context or earlier results)."""
    head = re.split(r"\n\n(?:Context from|Results from|Evidence you may)", task, maxsplit=1)[0]
    bullets = [line[2:].strip() for line in head.splitlines() if line.startswith("- ")]
    return bullets or [head.splitlines()[0].strip()]


def _evidence(request: ModelRequest) -> dict[str, str]:
    seen: dict[str, str] = dict(tool_evidence(request.input))
    for item in request.history:
        if isinstance(item, ToolResultsTurn):
            for result in item.results:
                seen.update(tool_evidence(result.content))
    return seen


def _issued(request: ModelRequest) -> list[ToolCall]:
    return [
        call
        for item in request.history
        if isinstance(item, AssistantTurn)
        for call in item.tool_calls
    ]


@dataclass
class SimAgent:
    deployment: str
    role: str = "worker"
    parallel: bool = False
    fail: bool = False
    """Every call fails as an authentication error (a misconfigured deployment)."""
    always_delegate: int = 0
    """Delegate this many copies of the task whenever a delegate tool is offered (runaway)."""
    hold: asyncio.Event | None = None
    """Block worker calls until set (to interrupt the process mid-delegation)."""
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    """Set when a call is blocked on `hold`."""
    deduplicates: bool = True
    """Declared like a deterministic deployment (retry-safe); False behaves like a real
    provider: an interrupted call's outcome is ambiguous and, by default, not retried."""
    calls: int = 0

    @property
    def descriptor(self) -> DeploymentDescriptor:
        return DeploymentDescriptor(
            deployment=self.deployment,
            provider="simulation",
            protocol="hollis-simulated-agent",
            model=f"sim-{self.role}",
            locality=Locality.LOCAL,
            deduplicates_requests=self.deduplicates,
            capabilities=ModelCapabilities(
                text=TextCapabilities(tools=True, context_window_tokens=200_000)
            ),
        )

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.fail:
            raise ModelCallError(FailureKind.AUTHENTICATION, "simulated: invalid credentials")
        if self.hold is not None and self.role == "worker":
            self.entered.set()
            await self.hold.wait()
        policy: Callable[[ModelRequest], ModelResponse] = {
            "worker": self._worker,
            "lead": self._lead,
            "reviewer": self._reviewer,
        }[self.role]
        response = policy(request)
        if request.instructions and "Report protocol (Hollis)" in request.instructions:
            response = _with_self_report(response)
        prompt = "".join(
            (
                request.instructions or "",
                request.input,
                *(b.text for b in request.context),
                *(json.dumps(t.input_schema) + t.description for t in request.tools),
                *(
                    i.content
                    if isinstance(i, AssistantTurn)
                    else "".join(r.content for r in i.results)
                    for i in request.history
                ),
            )
        )
        output = response.content + "".join(json.dumps(c.arguments) for c in response.tool_calls)
        usage = Usage(input_tokens=estimate_text(prompt), output_tokens=estimate_text(output) + 1)
        await asyncio.sleep(
            BASE_LATENCY_S
            + PER_INPUT_TOKEN_S * usage.input_tokens
            + PER_OUTPUT_TOKEN_S * usage.output_tokens
        )
        return response.model_copy(update={"usage": usage, "served_model": f"sim-{self.role}"})

    # --- policies --------------------------------------------------------------------------------

    def _worker(self, request: ModelRequest) -> ModelResponse:
        tools = {t.name for t in request.tools}
        delegates = sorted(t for t in tools if t.startswith("delegate_"))
        if self.always_delegate and delegates and not request.history:
            return _calls(
                *(
                    ToolCall(
                        call_id=f"copy{i}",
                        name=delegates[0],
                        arguments={"objective": questions_in(request.input)[0]},
                    )
                    for i in range(self.always_delegate)
                )
            )
        questions = questions_in(request.input)
        prior = " ".join(body for _, _, body in extract_reports(request.input))
        searched = [c.arguments.get("query") for c in _issued(request) if c.arguments]
        known = self._answers(questions, _evidence(request), prior)
        if "knowledge_search" in tools:
            pending = [q for q in questions if not any(str(s).startswith(q) for s in searched)]
            if pending:
                batch = pending if self.parallel else pending[:1]
                return _calls(
                    *(
                        ToolCall(
                            call_id=f"s{len(searched) + i}",
                            name="knowledge_search",
                            arguments={"query": _query(q, prior, known), "limit": 3},
                        )
                        for i, q in enumerate(batch)
                    )
                )
        lines = [
            f"{sentence.rstrip('.')} [{handle}]." if sentence else f"The sources do not say: {q}"
            for q, (sentence, handle) in zip(questions, known, strict=True)
        ]
        return ModelResponse(content="\n".join(lines))

    def _answers(
        self, questions: list[str], evidence: dict[str, str], prior: str = ""
    ) -> list[tuple[str, str]]:
        answers: list[tuple[str, str]] = []
        for q in questions:
            hint = " ".join([prior, *(s for s, _ in answers)])
            found = best_sentence(q, evidence, hint=hint)
            answers.append(found or ("", ""))
        return answers

    def _lead(self, request: ModelRequest) -> ModelResponse:
        delegates = {t.name: t.description for t in request.tools if t.name.startswith("delegate_")}
        questions = questions_in(request.input)
        if not request.history:
            routed: dict[str, list[str]] = {}
            for q in questions:
                tool = max(delegates, key=lambda n: (len(_terms(q) & _terms(delegates[n])), n))
                routed.setdefault(tool, []).append(q)
            return _calls(
                *(
                    ToolCall(
                        call_id=f"d{i}",
                        name=tool,
                        arguments={"objective": "\n".join(f"- {q}" for q in qs)},
                    )
                    for i, (tool, qs) in enumerate(sorted(routed.items()))
                )
            )
        lines: list[str] = []
        for item in request.history:
            if not isinstance(item, ToolResultsTurn):
                continue
            for result in item.results:
                meta = report_meta(result.content) or {}
                handles = set(tool_evidence(result.content))
                bodies = [
                    restore_citations(b, handles) for _, _, b in extract_reports(result.content)
                ]
                if meta.get("outcome") == "completed":
                    lines += [line for body in bodies for line in body.splitlines() if line]
                else:
                    agent = meta.get("agent", "an agent")
                    lines.append(f"Not answered by {agent}: {meta.get('outcome')}.")
        return ModelResponse(content="\n".join(lines))

    def _reviewer(self, request: ModelRequest) -> ModelResponse:
        evidence = _evidence(request)
        handles = set(evidence)
        claims = [
            restore_citations(line, handles).strip()
            for _, _, body in extract_reports(request.input)
            for line in body.splitlines()
            if line.strip() and not line.startswith("{")
        ]
        if not claims:
            return ModelResponse(content="There were no reports to review.")
        years = [max((int(y) for y in _YEAR.findall(c)), default=0) for c in claims]
        newest = claims[years.index(max(years))]
        if len({re.sub(r"\[.*?\]", "", c) for c in claims}) == 1:
            return ModelResponse(content=newest)
        return ModelResponse(
            content=f"{newest}\nThe sources disagree; the most recent one is preferred."
        )


_REFERS_BACK = re.compile(r"\b(that|this|those|these|it|they|them)\b", re.I)


def _query(question: str, prior: str, known: list[tuple[str, str]]) -> str:
    """The question; plus results passed in from earlier steps, and, when the question refers
    back ("that team"), what earlier searches found, as a model resolving a reference would."""
    found = " ".join(s for s, _ in known if s) if _REFERS_BACK.search(question) else ""
    context = " ".join(x for x in (prior, found) if x).strip()
    return f"{question} {context}"[:400] if context else question


def _calls(*calls: ToolCall) -> ModelResponse:
    return ModelResponse(content="", tool_calls=calls, stop_reason=StopReason.TOOL_USE)


def _with_self_report(response: ModelResponse) -> ModelResponse:
    if response.tool_calls:
        return response
    first = response.content.splitlines()[0] if response.content else ""
    status = "partial" if "do not say" in response.content else "done"
    report = {"status": status, "answer": re.sub(r"\s*\[.*?\]", "", first), "confidence": "medium"}
    content = f"{response.content}\n\n```json\n{json.dumps(report)}\n```"
    return response.model_copy(update={"content": content})
