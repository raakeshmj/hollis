"""The synchronous Hollis client: the asynchronous client driven on a private event loop.

    with Hollis.open() as hollis:
        run = hollis.runs.start("list the files", agent="helper")
        print(run.wait().phase)

The loop runs on a background thread, so embedded runs keep executing between calls (as they
would in a server); `close()` (or leaving the `with` block) stops them, leaving them resumable.
"""

import asyncio
import builtins
import os
import threading
from collections.abc import AsyncIterator, Coroutine, Iterator, Mapping
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from hollis.platform.schemas import (
    Agent,
    AgentInput,
    Approval,
    ArtifactView,
    CollectionView,
    CreatedToken,
    DeploymentView,
    EventView,
    Health,
    IngestResultView,
    MaintenanceView,
    McpServerView,
    MemoryDetail,
    MemoryView,
    OrchestrationView,
    Page,
    Plan,
    PrincipalInput,
    PrincipalView,
    ProbeResult,
    RunDetail,
    RunPhase,
    RunSummary,
    SearchResultView,
    SettingsView,
    SourceDetail,
    SourceView,
    SyncResultView,
    TokenView,
    ToolActivity,
    ToolView,
    WriteResultView,
)
from hollis.runtime.knowledge import SourceTrust
from hollis.runtime.memory import MemoryKind
from hollis.sdk import client as aio


class _Portal:
    """An event loop on a daemon thread. Every client call runs there."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="hollis", daemon=True)
        self._thread.start()
        self._closed = False

    def call[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        if self._closed:
            coroutine.close()
            raise RuntimeError("this Hollis client is closed")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        try:
            return future.result()
        except BaseException:
            future.cancel()  # e.g. Ctrl-C while waiting: stop waiting (runs are unaffected)
            raise

    def iterate[T](self, iterator: AsyncIterator[T]) -> Iterator[T]:
        async def step() -> tuple[T] | None:
            try:
                return (await anext(iterator),)
            except StopAsyncIteration:
                return None

        try:
            while (item := self.call(step())) is not None:
                yield item[0]
        finally:
            closer = getattr(iterator, "aclose", None)
            if closer is not None and not self._closed:
                self.call(closer())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=30)
        self._loop.close()


class RunHandle:
    def __init__(self, portal: _Portal, handle: aio.RunHandle) -> None:
        self._portal = portal
        self._handle = handle

    @property
    def id(self) -> UUID:
        return self._handle.id

    @property
    def phase(self) -> RunPhase:
        return self._handle.phase

    @property
    def summary(self) -> RunSummary:
        return self._handle.summary

    def __repr__(self) -> str:
        return f"RunHandle({self.id}, {self.phase.value})"

    def refresh(self) -> RunSummary:
        return self._portal.call(self._handle.refresh())

    def wait(self, timeout_s: float = aio.DEFAULT_WAIT_S) -> RunSummary:
        return self._portal.call(self._handle.wait(timeout_s))

    def detail(self) -> RunDetail:
        return self._portal.call(self._handle.detail())

    def answer(self, timeout_s: float = aio.DEFAULT_WAIT_S) -> str | None:
        return self._portal.call(self._handle.answer(timeout_s))

    def events(self, after: int = 0) -> list[EventView]:
        return self._portal.call(self._handle.events(after))

    def stream(self, after: int = 0) -> Iterator[EventView]:
        return self._portal.iterate(self._handle.stream(after))

    def approvals(self) -> list[Approval]:
        return self._portal.call(self._handle.approvals())

    def approve(self, invocation_id: str | None = None, *, reason: str | None = None) -> RunSummary:
        return self._portal.call(self._handle.approve(invocation_id, reason=reason))

    def deny(self, invocation_id: str | None = None, *, reason: str | None = None) -> RunSummary:
        return self._portal.call(self._handle.deny(invocation_id, reason=reason))

    def cancel(self, reason: str = "") -> RunSummary:
        return self._portal.call(self._handle.cancel(reason))

    def resume(self) -> RunSummary:
        return self._portal.call(self._handle.resume())

    def artifact(self, artifact_id: str, *, offset: int = 0, length: int = 20_000) -> ArtifactView:
        return self._portal.call(self._handle.artifact(artifact_id, offset=offset, length=length))

    def tree(self) -> OrchestrationView:
        return self._portal.call(self._handle.tree())


class Runs:
    def __init__(self, portal: _Portal, runs: aio.Runs) -> None:
        self._portal = portal
        self._runs = runs

    def start(
        self,
        input: str,
        *,
        agent: str | None = None,
        model: str | None = None,
        tools: tuple[str, ...] = (),
        instructions: str | None = None,
        session: str | None = None,
        plan: Plan | None = None,
    ) -> RunHandle:
        handle = self._portal.call(
            self._runs.start(
                input,
                agent=agent,
                model=model,
                tools=tools,
                instructions=instructions,
                session=session,
                plan=plan,
            )
        )
        return RunHandle(self._portal, handle)

    def get(self, run_id: UUID | str) -> RunHandle:
        return RunHandle(self._portal, self._portal.call(self._runs.get(run_id)))

    def list(
        self,
        *,
        phase: RunPhase | None = None,
        agent: str | None = None,
        roots: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[RunSummary]:
        return self._portal.call(
            self._runs.list(phase=phase, agent=agent, roots=roots, limit=limit, offset=offset)
        )


class Approvals:
    def __init__(self, portal: _Portal, approvals: aio.Approvals) -> None:
        self._portal = portal
        self._approvals = approvals

    def list(self) -> builtins.list[Approval]:
        return self._portal.call(self._approvals.list())

    def approve(self, approval: Approval, *, reason: str | None = None) -> RunSummary:
        return self._portal.call(self._approvals.approve(approval, reason=reason))

    def deny(self, approval: Approval, *, reason: str | None = None) -> RunSummary:
        return self._portal.call(self._approvals.deny(approval, reason=reason))


class Agents:
    def __init__(self, portal: _Portal, agents: aio.Agents) -> None:
        self._portal = portal
        self._agents = agents

    def list(self) -> builtins.list[Agent]:
        return self._portal.call(self._agents.list())

    def get(self, name: str) -> Agent:
        return self._portal.call(self._agents.get(name))

    def create(self, spec: AgentInput | None = None, /, **fields: Any) -> Agent:
        return self._portal.call(self._agents.create(spec, **fields))

    def update(self, name: str, spec: AgentInput | None = None, /, **fields: Any) -> Agent:
        return self._portal.call(self._agents.update(name, spec, **fields))

    def delete(self, name: str) -> None:
        self._portal.call(self._agents.delete(name))

    def starter(self) -> Agent:
        return self._portal.call(self._agents.starter())


class Models:
    def __init__(self, portal: _Portal, models: aio.Models) -> None:
        self._portal = portal
        self._models = models

    def list(self) -> builtins.list[DeploymentView]:
        return self._portal.call(self._models.list())

    def get(self, deployment_id: str) -> DeploymentView:
        return self._portal.call(self._models.get(deployment_id))

    def put(self, deployment: Mapping[str, Any]) -> DeploymentView:
        return self._portal.call(self._models.put(deployment))

    def delete(self, deployment_id: str) -> None:
        self._portal.call(self._models.delete(deployment_id))

    def probe(self, deployment_id: str) -> ProbeResult:
        return self._portal.call(self._models.probe(deployment_id))


class Tools:
    def __init__(self, portal: _Portal, tools: aio.Tools) -> None:
        self._portal = portal
        self._tools = tools

    def list(self) -> builtins.list[ToolView]:
        return self._portal.call(self._tools.list())

    def activity(self, limit: int = 50) -> builtins.list[ToolActivity]:
        return self._portal.call(self._tools.activity(limit))

    def mcp_servers(self) -> builtins.list[McpServerView]:
        return self._portal.call(self._tools.mcp_servers())

    def connect_mcp(self, name: str) -> McpServerView:
        return self._portal.call(self._tools.connect_mcp(name))


class Knowledge:
    def __init__(self, portal: _Portal, knowledge: aio.Knowledge) -> None:
        self._portal = portal
        self._knowledge = knowledge

    def collections(self) -> list[CollectionView]:
        return self._portal.call(self._knowledge.collections())

    def create_collection(
        self, name: str, *, description: str = "", embedded: bool = True
    ) -> CollectionView:
        return self._portal.call(
            self._knowledge.create_collection(name, description=description, embedded=embedded)
        )

    def delete_collection(self, name: str) -> None:
        self._portal.call(self._knowledge.delete_collection(name))

    def sources(self, collection: str) -> list[SourceView]:
        return self._portal.call(self._knowledge.sources(collection))

    def source(self, collection: str, source_id: str) -> SourceDetail:
        return self._portal.call(self._knowledge.source(collection, source_id))

    def ingest(
        self,
        collection: str,
        key: str,
        data: bytes | str,
        *,
        filename: str | None = None,
        labels: tuple[str, ...] = (),
        trust: SourceTrust = SourceTrust.INTERNAL,
    ) -> IngestResultView:
        return self._portal.call(
            self._knowledge.ingest(
                collection, key, data, filename=filename, labels=labels, trust=trust
            )
        )

    def ingest_file(
        self,
        collection: str,
        path: str | os.PathLike[str],
        *,
        key: str | None = None,
        labels: tuple[str, ...] = (),
        trust: SourceTrust = SourceTrust.INTERNAL,
    ) -> IngestResultView:
        return self._portal.call(
            self._knowledge.ingest_file(collection, path, key=key, labels=labels, trust=trust)
        )

    def delete(self, collection: str, key: str) -> None:
        self._portal.call(self._knowledge.delete(collection, key))

    def relabel(self, collection: str, key: str, labels: tuple[str, ...]) -> SourceView:
        return self._portal.call(self._knowledge.relabel(collection, key, labels))

    def sync(
        self,
        collection: str,
        path: str,
        *,
        labels: tuple[str, ...] = (),
        trust: SourceTrust = SourceTrust.INTERNAL,
    ) -> SyncResultView:
        return self._portal.call(self._knowledge.sync(collection, path, labels=labels, trust=trust))

    def search(
        self, query: str, *, collections: tuple[str, ...] = (), limit: int = 8
    ) -> SearchResultView:
        return self._portal.call(
            self._knowledge.search(query, collections=collections, limit=limit)
        )

    def reconcile(self) -> dict[str, int]:
        return self._portal.call(self._knowledge.reconcile())


class Memory:
    def __init__(self, portal: _Portal, memory: aio.Memory) -> None:
        self._portal = portal
        self._memory = memory

    def list(
        self,
        *,
        scope: str | None = None,
        kind: MemoryKind | None = None,
        status: str | None = None,
        query: str | None = None,
        limit: int = 200,
    ) -> builtins.list[MemoryView]:
        return self._portal.call(
            self._memory.list(scope=scope, kind=kind, status=status, query=query, limit=limit)
        )

    def scopes(self) -> builtins.list[str]:
        return self._portal.call(self._memory.scopes())

    def get(self, memory_id: str) -> MemoryDetail:
        return self._portal.call(self._memory.get(memory_id))

    def remember(
        self,
        content: str,
        *,
        scope: str,
        kind: MemoryKind = MemoryKind.FACT,
        subject: str | None = None,
        key: str | None = None,
    ) -> WriteResultView:
        return self._portal.call(
            self._memory.remember(content, scope=scope, kind=kind, subject=subject, key=key)
        )

    def retract(self, memory_id: str, *, reason: str) -> MemoryView:
        return self._portal.call(self._memory.retract(memory_id, reason=reason))

    def forget(self, memory_id: str, *, reason: str) -> None:
        self._portal.call(self._memory.forget(memory_id, reason=reason))

    def maintain(self) -> MaintenanceView:
        return self._portal.call(self._memory.maintain())


class Tokens:
    def __init__(self, portal: _Portal, tokens: aio.Tokens) -> None:
        self._portal = portal
        self._tokens = tokens

    def list(self) -> builtins.list[TokenView]:
        return self._portal.call(self._tokens.list())

    def create(
        self,
        principal: str | None = None,
        *,
        label: str = "",
        expires_in_days: float | None = None,
    ) -> CreatedToken:
        return self._portal.call(
            self._tokens.create(principal, label=label, expires_in_days=expires_in_days)
        )

    def revoke(self, token_id: str) -> None:
        self._portal.call(self._tokens.revoke(token_id))


class Principals:
    def __init__(self, portal: _Portal, principals: aio.Principals) -> None:
        self._portal = portal
        self._principals = principals

    def list(self) -> builtins.list[PrincipalView]:
        return self._portal.call(self._principals.list())

    def create(self, spec: PrincipalInput | None = None, /, **fields: Any) -> PrincipalView:
        return self._portal.call(self._principals.create(spec, **fields))


class Hollis:
    def __init__(self, client: aio.AsyncHollis, portal: _Portal) -> None:
        self.aio = client
        """The underlying asynchronous client (its calls must run on this client's loop)."""
        self._portal = portal
        self.runs = Runs(portal, client.runs)
        self.approvals = Approvals(portal, client.approvals)
        self.agents = Agents(portal, client.agents)
        self.models = Models(portal, client.models)
        self.tools = Tools(portal, client.tools)
        self.knowledge = Knowledge(portal, client.knowledge)
        self.memory = Memory(portal, client.memory)
        self.tokens = Tokens(portal, client.tokens)
        self.principals = Principals(portal, client.principals)

    @classmethod
    def open(cls, workspace: str | os.PathLike[str] | None = None) -> Self:
        """The workspace at `workspace` (default: found from the current directory or
        HOLLIS_WORKSPACE), embedded in this process and used as its local admin."""
        return cls._start(aio.AsyncHollis.open(workspace))

    @classmethod
    def connect(cls, url: str, token: str, **options: Any) -> Self:
        """A Hollis server, as the principal `token` belongs to."""

        async def connect() -> aio.AsyncHollis:
            return aio.AsyncHollis.connect(url, token, **options)

        return cls._start(connect())

    @classmethod
    def from_env(cls, workspace: str | os.PathLike[str] | None = None) -> Self:
        """HOLLIS_URL and HOLLIS_TOKEN if set (a server), otherwise the local workspace."""
        return cls._start(aio.AsyncHollis.from_env(workspace))

    @classmethod
    def _start(cls, opening: Coroutine[Any, Any, aio.AsyncHollis]) -> Self:
        portal = _Portal()
        try:
            return cls(portal.call(opening), portal)
        except BaseException:
            portal.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._portal.call(self.aio.close())
        finally:
            self._portal.close()

    @property
    def location(self) -> str:
        return self.aio.backend.location

    def run_coroutine[T](self, coroutine: Coroutine[Any, Any, T]) -> T:
        """Run a coroutine that uses `self.aio` on this client's event loop."""
        return self._portal.call(coroutine)

    def health(self) -> Health:
        return self._portal.call(self.aio.health())

    def settings(self) -> SettingsView:
        return self._portal.call(self.aio.settings())

    def update_config(self, config: Mapping[str, Any]) -> SettingsView:
        return self._portal.call(self.aio.update_config(config))

    def whoami(self) -> PrincipalView:
        return self._portal.call(self.aio.whoami())

    def audit(self, limit: int = 100) -> list[dict[str, str]]:
        return self._portal.call(self.aio.audit(limit))

    def login_token(self) -> str:
        return self._portal.call(self.aio.login_token())
