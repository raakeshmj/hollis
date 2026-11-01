"""Helpers for orchestration tests: a team of demo-model agents over two collections, and
in-process "agent models" whose behaviour a test scripts as a function of the request."""

from collections.abc import Awaitable, Callable
from typing import Any

from hollis.platform.auth import Principal
from hollis.platform.schemas import AgentInput, CollectionInput, StartRun
from hollis.platform.services import Platform
from hollis.runtime.conversation import ToolCall, ToolResultsTurn
from hollis.runtime.delegation import DelegationOverrides
from hollis.runtime.knowledge import KnowledgeAccess
from hollis.runtime.model import (
    DeploymentDescriptor,
    Locality,
    ModelCapabilities,
    ModelRequest,
    ModelResponse,
    StopReason,
    TextCapabilities,
    Usage,
)

HANDBOOK = (
    "# Handbook\n\n## Deploying\n\nThe deploy command is make ship and it needs the VPN.\n\n"
    "## Support\n\nEscalations go to the on-call engineer.\n"
)
FINANCE = "# Finance\n\n## Budget\n\nThe travel budget for 2026 is 41000 euros per team.\n"
FINANCE_SECRET = "41000 euros"

type Behaviour = Callable[[ModelRequest], ModelResponse | Awaitable[ModelResponse]]


class ScriptedAgentModel:
    """A deterministic in-process deployment for orchestration tests."""

    def __init__(self, deployment: str, behaviour: Behaviour, *, tokens: int = 10) -> None:
        self._descriptor = DeploymentDescriptor(
            deployment=deployment,
            provider="test",
            protocol="test-agent",
            model=f"{deployment}-1",
            locality=Locality.LOCAL,
            deduplicates_requests=True,
            capabilities=ModelCapabilities(
                text=TextCapabilities(tools=True, context_window_tokens=200_000)
            ),
        )
        self._behaviour = behaviour
        self.tokens = tokens
        self.requests: list[ModelRequest] = []

    @property
    def descriptor(self) -> DeploymentDescriptor:
        return self._descriptor

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        result = self._behaviour(request)
        response = result if isinstance(result, ModelResponse) else await result
        return response.model_copy(
            update={"usage": Usage(input_tokens=self.tokens, output_tokens=0)}
        )


def delegate(agent: str, objective: str, call_id: str = "d1", **extra: Any) -> ToolCall:
    return ToolCall(
        call_id=call_id,
        name="delegate_" + agent.replace("-", "_"),
        arguments={"objective": objective, **extra},
    )


def calls(*items: ToolCall) -> ModelResponse:
    return ModelResponse(content="", tool_calls=items, stop_reason=StopReason.TOOL_USE)


def answer(text: str) -> ModelResponse:
    return ModelResponse(content=text)


def first_turn(request: ModelRequest) -> bool:
    return not request.history


def tool_results(request: ModelRequest) -> list[str]:
    return [
        r.content
        for item in request.history
        if isinstance(item, ToolResultsTurn)
        for r in item.results
    ]


async def add_team(
    platform: Platform,
    admin: Principal,
    *,
    lead_tools: tuple[str, ...] = (),
    delegation: DelegationOverrides | None = None,
) -> None:
    """Collections docs and finance; agents researcher (docs), analyst (finance), reviewer,
    writer (file writes) and lead, which may delegate to all of them."""
    for name, text in (("docs", HANDBOOK), ("finance", FINANCE)):
        await platform.create_collection(admin, CollectionInput(name=name))
        await platform.ingest(admin, name, key=f"{name}.md", data=text.encode())

    async def agent(name: str, **fields: Any) -> None:
        await platform.create_agent(
            admin, AgentInput(name=name, model="demo", description=f"the {name}", **fields)
        )

    await agent(
        "researcher",
        tools=("knowledge_search",),
        knowledge=KnowledgeAccess(collections=("docs",)),
    )
    await agent(
        "analyst",
        tools=("knowledge_search",),
        knowledge=KnowledgeAccess(collections=("finance",)),
    )
    await agent("reviewer")
    await agent("writer", tools=("fs_write_file",))
    await agent(
        "lead",
        tools=lead_tools,
        knowledge=KnowledgeAccess(collections=("docs", "finance")),
        delegates=("researcher", "analyst", "reviewer", "writer"),
        delegation=delegation,
    )


async def run_lead(platform: Platform, principal: Principal, task: str, agent: str = "lead") -> Any:
    started = await platform.start_run(principal, StartRun(input=task, agent=agent))
    return await platform.wait_run(principal, started.run_id, timeout_s=20)
