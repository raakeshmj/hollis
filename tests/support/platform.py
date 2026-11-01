"""Helpers for platform tests: a workspace with the demo model, principals with grants."""

from pathlib import Path

from hollis.platform.auth import Principal
from hollis.platform.schemas import (
    AgentInput,
    PrincipalInput,
    Role,
    RunSummary,
    StartRun,
    TokenInput,
)
from hollis.platform.services import Platform
from hollis.runtime.knowledge import KnowledgeAccess
from hollis.runtime.memory import MemoryAccess

README = (
    "# Handbook\n\n"
    "## Deploying\n\n"
    "The deploy command is `make ship`. It needs the VPN.\n\n"
    "## Support\n\n"
    "Escalations go to the on-call engineer.\n"
)
DEMO_TOOLS = (
    "fs_write_file",
    "fs_read_file",
    "fs_list_dir",
    "knowledge_search",
    "memory_remember",
    "memory_recall",
)


async def add_principal(
    platform: Platform,
    admin: Principal,
    name: str,
    role: Role = Role.OPERATOR,
    *,
    can_approve: bool = False,
    memory: MemoryAccess | None = None,
    knowledge: KnowledgeAccess | None = None,
) -> Principal:
    """Create a principal and authenticate as it with a fresh API token."""
    spec = PrincipalInput(
        name=name, role=role, can_approve=can_approve, memory=memory, knowledge=knowledge
    )
    await platform.create_principal(admin, spec)
    token = await platform.create_token(admin, TokenInput(principal=name, label="test"))
    return await platform.authenticate(token.token)


async def add_agent(
    platform: Platform,
    admin: Principal,
    name: str = "helper",
    *,
    tools: tuple[str, ...] = DEMO_TOOLS,
    memory: MemoryAccess | None = None,
    knowledge: KnowledgeAccess | None = None,
    record_episodes: bool = False,
) -> None:
    await platform.create_agent(
        admin,
        AgentInput(
            name=name,
            model="demo",
            instructions="Help the team.",
            tools=tools,
            memory=memory,
            knowledge=knowledge,
            record_episodes=record_episodes,
        ),
    )


async def run_to_rest(
    platform: Platform,
    principal: Principal,
    task: str,
    *,
    agent: str = "helper",
    session: str | None = None,
) -> RunSummary:
    """Start a run and wait until it finishes or waits for approval."""
    started = await platform.start_run(
        principal, StartRun(input=task, agent=agent, session=session)
    )
    return await platform.wait_run(principal, started.run_id, timeout_s=10)


def write_readme(root: Path) -> bytes:
    data = README.encode()
    (root / "HANDBOOK.md").write_bytes(data)
    return data
