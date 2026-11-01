"""The workspace: composes M0-M5 into one running platform (ADR-0026).

`Workspace.open(root)` opens the stores in `.hollis/` and builds, from `hollis.toml`:
- the model gateway (plus the built-in demo model) and the knowledge embedder;
- the tool catalog: filesystem tools (if configured), artifact, knowledge and memory tools, MCP
  servers (connected on demand), and one `delegate_<agent>` tool per agent (ADR-0030);
- the tool policy (configured rules, falling back to the fail-safe default);
- one `Runner` with deferred approvals (runs wait for a decision instead of being denied) and
  observers that keep the run index current and wake live streams;
- the run supervisor.

There is exactly one implementation of every behaviour: services, the REST API, the SDK and the
CLI all go through this object.
"""

import asyncio
import contextlib
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from hollis.adapters.artifact_store import SqliteArtifactStore
from hollis.adapters.demo_model import DEMO_DEPLOYMENT, DemoModelProvider
from hollis.adapters.local_embeddings import HashingEmbedder
from hollis.adapters.sqlite_knowledge import SqliteKnowledgeStore
from hollis.adapters.sqlite_memory import SqliteMemoryStore
from hollis.adapters.sqlite_store import SqliteRunStore
from hollis.gateway import Catalog, ModelGateway
from hollis.knowledge.ingest import KnowledgeBase
from hollis.knowledge.retrieve import KnowledgeRetriever
from hollis.knowledge.tools import knowledge_tools
from hollis.memory import MemoryManager, memory_tools, record_episode
from hollis.platform.auth import Auth
from hollis.platform.config import DATA_DIR, WorkspaceConfig, load_config, save_config
from hollis.platform.coordinator import WORKFLOW_DEPLOYMENT, WorkflowCoordinator
from hollis.platform.db import WorkspaceDb
from hollis.platform.orchestration import DelegateTool, Orchestrator, delegation_effect
from hollis.platform.schemas import Agent
from hollis.platform.supervisor import EventBus, RunSupervisor
from hollis.runtime.embedding import EmbeddingProvider
from hollis.runtime.events import EventEnvelope, ModelCallFailed, ModelResponseRecorded
from hollis.runtime.model import ModelProvider
from hollis.runtime.runner import Runner
from hollis.runtime.state import RunState, fold
from hollis.runtime.store import load_run
from hollis.runtime.tools import Tool, ToolEffect, ToolRef
from hollis.tools import CallbackApprover, Rule, RulePolicy, ToolRegistry
from hollis.tools.builtin import artifact_tools, filesystem_tools
from hollis.tools.mcp_config import McpServerConfig

log = logging.getLogger("hollis.platform")

LOCAL_EMBEDDER = "local-hashing"
_CALL_ENDED = (ModelResponseRecorded, ModelCallFailed)
_TERMINAL = ("completed", "failed", "cancelled")


class PlatformResolver:
    """The gateway, plus the built-in demo deployment when enabled and the deterministic
    workflow coordinator. `register` adds providers for embedded use and tests."""

    def __init__(self, gateway: ModelGateway, *, demo: bool) -> None:
        self._gateway = gateway
        self._demo = DemoModelProvider() if demo else None
        self._builtin: dict[str, ModelProvider] = {WORKFLOW_DEPLOYMENT: WorkflowCoordinator()}

    def register(self, provider: ModelProvider) -> None:
        self._builtin[provider.descriptor.deployment] = provider

    def builtin(self, model: str) -> bool:
        return model in self._builtin or (self._demo is not None and model == DEMO_DEPLOYMENT)

    def resolve(self, model: str) -> ModelProvider:
        if self._demo is not None and model == DEMO_DEPLOYMENT:
            return self._demo
        if model in self._builtin:
            return self._builtin[model]
        return self._gateway.resolve(model)

    @property
    def demo(self) -> DemoModelProvider | None:
        return self._demo


class McpConnection:
    def __init__(self, config: McpServerConfig) -> None:
        self.config = config
        self.server: Any = None
        self.status = "not_connected"
        self.error: str | None = None
        self.connected_at: datetime | None = None


class WorkspaceTools:
    """The runner's tool catalog: registered tools, then delegate tools."""

    def __init__(self, workspace: "Workspace") -> None:
        self._ws = workspace

    def get(self, name: str) -> Tool | None:
        return self._ws.registry.get(name) or self._ws.delegate_tools.get(name)


class Workspace:
    def __init__(
        self,
        root: Path,
        config: WorkspaceConfig,
        *,
        db: WorkspaceDb,
        runs: SqliteRunStore,
        artifacts: SqliteArtifactStore,
        memory_store: SqliteMemoryStore,
        knowledge_store: SqliteKnowledgeStore,
    ) -> None:
        self.root = root
        self.config = config
        self.db = db
        self.auth = Auth(db)
        self.runs = runs
        self.artifacts = artifacts
        self.memory_store = memory_store
        self.knowledge_store = knowledge_store
        self.bus = EventBus()
        self.mcp: dict[str, McpConnection] = {}
        self._mcp_lock = asyncio.Lock()
        self.agents: dict[str, Agent] = {}
        self.delegate_tools: dict[str, DelegateTool] = {}
        self.orchestrator = Orchestrator(self)
        self.tools = WorkspaceTools(self)
        self._providers: list[ModelProvider] = []
        self._build()
        self.supervisor = RunSupervisor(
            lambda: self.runner, self.bus, on_error=self._record_error, prepare=self._prepare_run
        )

    @classmethod
    async def open(cls, root: Path) -> "Workspace":
        root, config = await asyncio.to_thread(_prepare, root)
        data = root / DATA_DIR
        workspace = cls(
            root,
            config,
            db=await WorkspaceDb.open(data / "workspace.db"),
            runs=await SqliteRunStore.open(data / "runs.db"),
            artifacts=await SqliteArtifactStore.open(data / "artifacts.db"),
            memory_store=await SqliteMemoryStore.open(data / "memory.db"),
            knowledge_store=await SqliteKnowledgeStore.open(data / "knowledge.db"),
        )
        await workspace.auth.ensure_admin()
        await workspace.load_agents()
        return workspace

    # --- composition ----------------------------------------------------------------------------

    def _build(self) -> None:
        config = self.config
        self.gateway = ModelGateway(Catalog(deployments=config.deployments, routing=config.routing))
        self.redactor = self.gateway.redactor
        self.resolver = PlatformResolver(self.gateway, demo=config.demo)
        for provider in self._providers:
            self.resolver.register(provider)
        self.embedder = self._embedder()
        self.memory = MemoryManager(self.memory_store, redactor=self.redactor)
        self.knowledge = KnowledgeBase(
            self.knowledge_store, embedder=self.embedder, redactor=self.redactor
        )
        self.retriever = KnowledgeRetriever(self.knowledge_store, embedder=self.embedder)
        self.registry = ToolRegistry()
        self.tool_sources: dict[str, str] = {}
        self._add_tools("builtin:artifacts", artifact_tools(self.artifacts))
        self._add_tools("builtin:knowledge", knowledge_tools(self.knowledge_store, self.retriever))
        self._add_tools(
            "builtin:memory", memory_tools(self.memory, allow_forget=config.tools.memory_forget)
        )
        fs = config.tools.filesystem
        if fs is not None:
            root = (self.root / fs.root).resolve()
            if root.is_dir():
                self._add_tools("builtin:filesystem", filesystem_tools(root, writable=fs.writable))
            else:
                log.warning("filesystem tools disabled: %s is not a directory", root)
        # Connections survive a rebuild unless their configuration changed (see apply_config).
        self.mcp = {c.name: self.mcp.get(c.name) or McpConnection(c) for c in config.tools.mcp}
        for connection in self.mcp.values():
            if connection.server is not None:
                self._add_tools(
                    f"mcp:{connection.config.name}", list(connection.server.tools.values())
                )
        rules = [
            Rule(
                decision=r.decision,
                tools=r.tools,
                effects=frozenset(r.effects) or None,
                trust=frozenset(r.trust) or None,
                environments=frozenset(r.environments) or None,
                delegated=r.delegated,
                name=r.name,
                reason=r.reason,
            )
            for r in config.policy.rules
        ]
        self.policy = RulePolicy.of(*rules)
        self.refresh_delegations()
        self.runner = Runner(
            self.runs,
            self.resolver,
            tools=self.tools,
            policy=self.policy,
            approver=CallbackApprover(lambda request: None),  # decisions come from people
            environment=config.runtime.environment,
            redactor=self.redactor,
            artifacts=self.artifacts,
            memory=self.memory,
            knowledge=self.retriever,
            observers=(self._observe, self.bus.publish),
            guard=self.orchestrator,
        )

    def register_provider(self, provider: ModelProvider) -> None:
        """Serve a deployment from an in-process provider (embedded use, tests, evaluations)."""
        self._providers.append(provider)
        self.resolver.register(provider)

    async def load_agents(self) -> None:
        rows = await self.db.query("SELECT data FROM agents ORDER BY name")
        self.agents = {a.name: a for a in (Agent.model_validate_json(r[0]) for r in rows)}
        self.refresh_delegations()

    def refresh_delegations(self) -> None:
        """One delegate tool per agent; its effect is the strongest its delegation can cause."""
        self.delegate_tools = self._delegate_tools(self.agents)

    def preview_delegations(self, agents: Mapping[str, Agent]) -> dict[str, ToolRef]:
        """The delegate tool identities a set of agent definitions would produce."""
        return {name: tool.descriptor.ref for name, tool in self._delegate_tools(agents).items()}

    def _delegate_tools(self, agents: Mapping[str, Agent]) -> dict[str, DelegateTool]:
        tools: dict[str, DelegateTool] = {}
        for agent in agents.values():
            effect = delegation_effect(agent.name, agents, self._tool_effect)
            tool = DelegateTool(self.orchestrator, agent, effect)
            tools[tool.descriptor.name] = tool
        return tools

    def _tool_effect(self, name: str) -> ToolEffect | None:
        if name.startswith("mcp_"):
            return None  # external and connected on demand: treated as mutating
        tool = self.registry.get(name)
        return tool.descriptor.effect if tool is not None else None

    async def connect_tools(self, tools: tuple[str, ...]) -> None:
        """Connect the MCP servers whose tools a run is about to use."""
        for tool in tools:
            server = self.mcp_server(tool)
            if server is not None and self.registry.get(tool) is None:
                await self.connect_mcp(server)

    def mcp_server(self, tool: str) -> str | None:
        """The configured MCP server a tool name belongs to (tools are named mcp_<server>_*)."""
        matches = [s for s in self.mcp if tool.startswith(f"mcp_{s}_")]
        return max(matches, key=len) if matches else None

    def _embedder(self) -> EmbeddingProvider:
        name = self.config.knowledge.embedder
        if name == LOCAL_EMBEDDER:
            return HashingEmbedder(dimensions=512, deployment=LOCAL_EMBEDDER)
        return self.gateway.embedder(name)

    def _add_tools(self, source: str, tools: list[Tool]) -> None:
        for tool in tools:
            name = tool.descriptor.name
            if self.registry.get(name) is not None:
                self.registry.remove(name)
            self.registry.add(tool)
            self.tool_sources[name] = source

    async def apply_config(self, config: WorkspaceConfig) -> None:
        """Persist a new configuration and rebuild everything derived from it. Runs in flight
        keep going; the next execution pass uses the new runner."""
        await asyncio.to_thread(save_config, self.root, config)
        old_gateway = self.gateway
        wanted = {c.name: c for c in config.tools.mcp}
        for name, connection in list(self.mcp.items()):
            if wanted.get(name) != connection.config:
                del self.mcp[name]
                if connection.server is not None:
                    with contextlib.suppress(Exception):
                        await connection.server.aclose()
        self.config = config
        self._build()
        with contextlib.suppress(Exception):
            await old_gateway.aclose()

    async def reload(self) -> None:
        await self.apply_config(await asyncio.to_thread(load_config, self.root))

    # --- MCP ------------------------------------------------------------------------------------

    async def connect_mcp(self, name: str) -> McpConnection:
        connection = self.mcp.get(name)
        if connection is None:
            raise LookupError(f"no MCP server {name!r} in the configuration")
        async with self._mcp_lock:
            return await self._connect(name, connection)

    async def _connect(self, name: str, connection: McpConnection) -> McpConnection:
        try:
            from hollis.tools.mcp import McpServer
        except ImportError as exc:
            connection.status, connection.error = (
                "error",
                f"MCP support is not installed ({exc.name})",
            )
            return connection
        try:
            if connection.server is not None:
                await connection.server.aclose()
            server = McpServer(connection.config, environ=os.environ, redactor=self.redactor)
            await server.connect()
        except Exception as exc:  # connection problems are shown, not raised
            connection.status, connection.error = (
                "error",
                self.redactor.redact(f"{type(exc).__name__}: {exc}")[:500],
            )
            return connection
        connection.server, connection.status, connection.error = server, "connected", None
        connection.connected_at = datetime.now(UTC)
        self._add_tools(f"mcp:{name}", list(server.tools.values()))
        self.refresh_delegations()
        return connection

    async def _prepare_run(self, run_id: UUID) -> None:
        """Connect MCP servers whose tools the run uses, before executing it."""
        state = await load_run(self.runs, run_id)
        for ref in state.tools:
            if ref.name.startswith("mcp_") and self.registry.get(ref.name) is None:
                for name, connection in self.mcp.items():
                    if ref.name.startswith(f"mcp_{name}_") and connection.status != "connected":
                        await self.connect_mcp(name)

    # --- run index ------------------------------------------------------------------------------

    async def _observe(self, event: EventEnvelope, state: RunState) -> None:
        try:
            await self._index(event, state)
        finally:  # after indexing, so the call's usage counts before its reservation goes
            if isinstance(event.payload, _CALL_ENDED) or state.status.is_terminal:
                self.orchestrator.release(state.run_id)
        if state.status.is_terminal and event.sequence == state.version:
            await self._record_episode(state)

    async def _record_episode(self, state: RunState) -> None:
        """For agents with record_episodes: one episode memory per finished run, written to the
        scope chosen at start (the first scope the run may write)."""
        row = await self.db.index_get(state.run_id)
        scope = row["data"].get("episode_scope") if row else None
        access = state.memory_access
        if not scope or access is None or scope not in access.write:
            return
        try:
            await record_episode(self.memory, state, scope, access)
        except Exception as exc:  # an episode is a convenience; the run itself is finished
            log.warning("episode for run %s not recorded: %s", state.run_id, exc)

    async def _index(self, event: EventEnvelope, state: RunState) -> None:
        failure = state.failure.message if state.failure else None
        lineage = state.lineage
        await self.db.index_upsert(
            state.run_id,
            status=state.status.value,
            created_at=state.created_at,
            updated_at=event.occurred_at,
            version=state.version,
            awaiting=len(state.awaiting_approval),
            data={
                "model": state.model,
                "provider": state.deployment.provider,
                "protocol": state.deployment.protocol,
                "input_preview": " ".join(state.input.split())[:160],
                "turns": len(state.turns),
                "tool_calls": sum(len(t.invocations) for t in state.turns),
                "usage": {
                    "input_tokens": state.usage.input_tokens,
                    "output_tokens": state.usage.output_tokens,
                },
                "error": failure,
                "stopped": None,  # any progress clears a previous execution error
                "objective": " ".join(state.assignment.objective.split())[:300]
                if state.assignment
                else None,
                "token_budget": state.delegation.token_budget if state.delegation else None,
            },
            parent_id=lineage.parent_run_id if lineage else None,
            root_id=lineage.root_run_id if lineage else None,
            depth=state.depth,
        )

    async def _record_error(self, run_id: UUID, message: str) -> None:
        """An execution pass stopped with an unexpected error: the run stays resumable."""
        self.orchestrator.release(run_id)  # its admitted model call, if any, is not in flight
        await self.db.index_patch(run_id, {"stopped": self.redactor.redact(message)})

    async def sync_index(self, *, rebuild: bool = False) -> int:
        """Bring run index rows up to date with the event logs, which are the source of truth
        (an index update can be lost if the process stops right after an append)."""
        indexed = {} if rebuild else await self.db.index_versions()
        count = 0
        offset = 0
        while True:
            listings = await self.runs.list_runs(limit=500, offset=offset)
            if not listings:
                return count
            for listing in listings:
                if indexed.get(listing.run_id) == listing.version:
                    continue
                events = await self.runs.read(listing.run_id)
                await self._index(events[-1], fold(events))
                count += 1
            offset += len(listings)

    async def recover(self) -> list[UUID]:
        """Resume runs that were interrupted (not waiting for approval, not terminal).

        A delegated run whose parent already finished is an orphan (the process stopped while
        a cancellation was cascading): it is cancelled, never resumed, so no child outlives
        the run that delegated it."""
        await self.sync_index()
        rows, _ = await self.db.index_list(owner=None, limit=10_000)
        status = {row["run_id"]: row["status"] for row in rows}
        resumed: list[UUID] = []
        for row in sorted(rows, key=lambda r: r["depth"]):
            if status[row["run_id"]] in _TERMINAL:
                continue
            parent = row["parent_id"]
            if parent is not None and status.get(parent) in _TERMINAL:
                reason = f"run {parent} that delegated it had already finished"
                state = await self.runner.cancel(row["run_id"], by="hollis", reason=reason)
                status[row["run_id"]] = state.status.value  # its own children cancel in turn
                continue
            if row["awaiting"]:
                continue
            if self.supervisor.schedule(row["run_id"]):
                resumed.append(row["run_id"])
        return resumed

    def storage(self) -> list[tuple[str, Path]]:
        data = self.root / DATA_DIR
        return [
            (name, data / f"{name}.db")
            for name in ("workspace", "runs", "memory", "knowledge", "artifacts")
        ]

    async def close(self) -> None:
        await self.supervisor.shutdown()
        for connection in self.mcp.values():
            if connection.server is not None:
                with contextlib.suppress(Exception):
                    await connection.server.aclose()
        with contextlib.suppress(Exception):
            await self.gateway.aclose()
        await asyncio.sleep(0)


def _prepare(root: Path) -> tuple[Path, WorkspaceConfig]:
    root = root.resolve()
    config = load_config(root)
    (root / DATA_DIR).mkdir(exist_ok=True)
    return root, config
