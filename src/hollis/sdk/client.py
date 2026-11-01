"""The asynchronous Hollis client.

    async with AsyncHollis.open() as hollis:                 # the workspace here (embedded)
    async with AsyncHollis.connect(url, token) as hollis:    # a Hollis server
        run = await hollis.runs.start("summarize the handbook", agent="helper")
        summary = await run.wait()
        if summary.phase is RunPhase.WAITING_APPROVAL:
            await run.approve()

The same calls, results and errors in both modes (`PlatformError` subclasses). See `Hollis` for
the synchronous client.
"""

import asyncio
import builtins
import os
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from hollis.platform.config import find_workspace
from hollis.platform.errors import InvalidRequest, NotFound
from hollis.platform.schemas import (
    Agent,
    AgentInput,
    Approval,
    ApprovalDecisionInput,
    ArtifactView,
    CancelInput,
    CollectionInput,
    CollectionView,
    CreatedToken,
    DeploymentInput,
    DeploymentView,
    EventView,
    ForgetInput,
    Health,
    IngestResultView,
    MaintenanceView,
    McpServerView,
    MemoryDetail,
    MemoryView,
    Page,
    PrincipalInput,
    PrincipalView,
    ProbeResult,
    RememberInput,
    RunDetail,
    RunPhase,
    RunSummary,
    SearchInput,
    SearchResultView,
    SettingsView,
    SourceDetail,
    SourceView,
    StartRun,
    SyncInput,
    SyncResultView,
    TokenInput,
    TokenView,
    ToolActivity,
    ToolView,
    WriteResultView,
)
from hollis.runtime.knowledge import SourceTrust
from hollis.runtime.memory import MemoryKind
from hollis.sdk.backend import Backend, LocalBackend

DEFAULT_WAIT_S = 300.0


class RunHandle:
    """A run: its last known summary, and what you can do with it."""

    def __init__(self, backend: Backend, summary: RunSummary) -> None:
        self._backend = backend
        self.summary = summary

    @property
    def id(self) -> UUID:
        return self.summary.run_id

    @property
    def phase(self) -> RunPhase:
        return self.summary.phase

    def __repr__(self) -> str:
        return f"RunHandle({self.id}, {self.phase.value})"

    async def refresh(self) -> RunSummary:
        self.summary = await self._backend.run_summary(self.id)
        return self.summary

    async def wait(self, timeout_s: float = DEFAULT_WAIT_S) -> RunSummary:
        """Wait until nothing drives the run: finished, waiting for approval, or stopped."""
        self.summary = await self._backend.wait_run(self.id, timeout_s)
        return self.summary

    async def detail(self) -> RunDetail:
        detail = await self._backend.run(self.id)
        self.summary = detail.summary
        return detail

    async def answer(self, timeout_s: float = DEFAULT_WAIT_S) -> str | None:
        """Wait, then return the final answer (None unless the run completed)."""
        await self.wait(timeout_s)
        return (await self.detail()).answer

    async def events(self, after: int = 0) -> list[EventView]:
        return await self._backend.run_events(self.id, after)

    def stream(self, after: int = 0) -> AsyncIterator[EventView]:
        """Events after `after`, then live events until the run finishes."""
        return self._backend.watch(self.id, after)

    async def approvals(self) -> list[Approval]:
        return [a for a in await self._backend.approvals() if a.run_id == self.id]

    async def approve(
        self, invocation_id: str | None = None, *, reason: str | None = None
    ) -> RunSummary:
        return await self._decide(invocation_id, approved=True, reason=reason)

    async def deny(
        self, invocation_id: str | None = None, *, reason: str | None = None
    ) -> RunSummary:
        return await self._decide(invocation_id, approved=False, reason=reason)

    async def _decide(
        self, invocation_id: str | None, *, approved: bool, reason: str | None
    ) -> RunSummary:
        if invocation_id is None:
            pending = await self.approvals()
            if len(pending) != 1:
                raise InvalidRequest(
                    f"run {self.id} has {len(pending)} pending approvals; name the invocation"
                )
            invocation_id = pending[0].invocation_id
        decision = ApprovalDecisionInput(approved=approved, reason=reason)
        self.summary = await self._backend.decide(self.id, invocation_id, decision)
        return self.summary

    async def cancel(self, reason: str = "") -> RunSummary:
        self.summary = await self._backend.cancel_run(self.id, CancelInput(reason=reason))
        return self.summary

    async def resume(self) -> RunSummary:
        self.summary = await self._backend.resume_run(self.id)
        return self.summary

    async def artifact(
        self, artifact_id: str, *, offset: int = 0, length: int = 20_000
    ) -> ArtifactView:
        return await self._backend.artifact(self.id, artifact_id, offset, length)


class Runs:
    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    async def start(
        self,
        input: str,
        *,
        agent: str | None = None,
        model: str | None = None,
        tools: tuple[str, ...] = (),
        instructions: str | None = None,
        session: str | None = None,
    ) -> RunHandle:
        """Start a run of an agent (or, for admins, of a model with tools) in the background."""
        spec = StartRun(
            input=input,
            agent=agent,
            model=model,
            tools=tools,
            instructions=instructions,
            session=session,
        )
        return RunHandle(self._backend, await self._backend.start_run(spec))

    async def get(self, run_id: UUID | str) -> RunHandle:
        summary = await self._backend.run_summary(_uuid(run_id))
        return RunHandle(self._backend, summary)

    async def list(
        self,
        *,
        phase: RunPhase | None = None,
        agent: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[RunSummary]:
        return await self._backend.runs(phase=phase, agent=agent, limit=limit, offset=offset)


class Approvals:
    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    async def list(self) -> list[Approval]:
        return await self._backend.approvals()

    async def approve(self, approval: Approval, *, reason: str | None = None) -> RunSummary:
        decision = ApprovalDecisionInput(approved=True, reason=reason)
        return await self._backend.decide(approval.run_id, approval.invocation_id, decision)

    async def deny(self, approval: Approval, *, reason: str | None = None) -> RunSummary:
        decision = ApprovalDecisionInput(approved=False, reason=reason)
        return await self._backend.decide(approval.run_id, approval.invocation_id, decision)


class Agents:
    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    async def list(self) -> list[Agent]:
        return await self._backend.agents()

    async def get(self, name: str) -> Agent:
        return await self._backend.agent(name)

    async def create(self, spec: AgentInput | None = None, /, **fields: Any) -> Agent:
        """`create(AgentInput(...))` or `create(name=..., model=..., tools=(...))`."""
        return await self._backend.create_agent(_spec(AgentInput, spec, fields))

    async def update(self, name: str, spec: AgentInput | None = None, /, **fields: Any) -> Agent:
        """Replace an agent's definition; with keyword fields, change only those."""
        if spec is None:
            current = (await self.get(name)).model_dump(include=set(AgentInput.model_fields))
            spec = AgentInput.model_validate({**current, **fields})
        return await self._backend.update_agent(name, spec)

    async def delete(self, name: str) -> None:
        await self._backend.delete_agent(name)

    async def starter(self) -> Agent:
        """Create the starter `docs` collection and default agent (admin; idempotent)."""
        return await self._backend.create_starter()


class Models:
    """Model deployments (built in, or configured in hollis.toml)."""

    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    async def list(self) -> list[DeploymentView]:
        return await self._backend.deployments()

    async def get(self, deployment_id: str) -> DeploymentView:
        return await self._backend.deployment(deployment_id)

    async def put(self, deployment: Mapping[str, Any]) -> DeploymentView:
        """Create or replace a deployment from its catalog definition (credentials by
        environment-variable name only)."""
        return await self._backend.upsert_deployment(DeploymentInput(deployment=dict(deployment)))

    async def delete(self, deployment_id: str) -> None:
        await self._backend.delete_deployment(deployment_id)

    async def probe(self, deployment_id: str) -> ProbeResult:
        """Send one tiny request to the deployment (a real network call; may cost money)."""
        return await self._backend.probe_deployment(deployment_id)


class Tools:
    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    async def list(self) -> list[ToolView]:
        return await self._backend.tools()

    async def activity(self, limit: int = 50) -> builtins.list[ToolActivity]:
        return await self._backend.tool_activity(limit)

    async def mcp_servers(self) -> builtins.list[McpServerView]:
        return await self._backend.mcp_servers()

    async def connect_mcp(self, name: str) -> McpServerView:
        return await self._backend.connect_mcp(name)


class Knowledge:
    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    async def collections(self) -> list[CollectionView]:
        return await self._backend.collections()

    async def create_collection(
        self, name: str, *, description: str = "", embedded: bool = True
    ) -> CollectionView:
        spec = CollectionInput(name=name, description=description, embedded=embedded)
        return await self._backend.create_collection(spec)

    async def delete_collection(self, name: str) -> None:
        await self._backend.delete_collection(name)

    async def sources(self, collection: str) -> list[SourceView]:
        return await self._backend.sources(collection)

    async def source(self, collection: str, source_id: str) -> SourceDetail:
        return await self._backend.source(collection, source_id)

    async def ingest(
        self,
        collection: str,
        key: str,
        data: bytes | str,
        *,
        filename: str | None = None,
        labels: tuple[str, ...] = (),
        trust: SourceTrust = SourceTrust.INTERNAL,
    ) -> IngestResultView:
        """Add or replace the document stored under `key`."""
        raw = data.encode() if isinstance(data, str) else data
        return await self._backend.ingest(
            collection, key, raw, filename=filename, labels=labels, trust=trust
        )

    async def ingest_file(
        self,
        collection: str,
        path: str | os.PathLike[str],
        *,
        key: str | None = None,
        labels: tuple[str, ...] = (),
        trust: SourceTrust = SourceTrust.INTERNAL,
    ) -> IngestResultView:
        """Read a local file (on this machine) and ingest it; the key defaults to its name."""
        file = Path(path)
        return await self.ingest(
            collection,
            key or file.name,
            await asyncio.to_thread(file.read_bytes),
            filename=file.name,
            labels=labels,
            trust=trust,
        )

    async def delete(self, collection: str, key: str) -> None:
        await self._backend.delete_source(collection, key)

    async def relabel(self, collection: str, key: str, labels: tuple[str, ...]) -> SourceView:
        return await self._backend.relabel(collection, key, labels)

    async def sync(
        self,
        collection: str,
        path: str,
        *,
        labels: tuple[str, ...] = (),
        trust: SourceTrust = SourceTrust.INTERNAL,
    ) -> SyncResultView:
        """Ingest a directory of the workspace (on the server, within knowledge.sync_roots)."""
        return await self._backend.sync(
            collection, SyncInput(path=path, labels=labels, trust=trust)
        )

    async def search(
        self, query: str, *, collections: tuple[str, ...] = (), limit: int = 8
    ) -> SearchResultView:
        return await self._backend.search(
            SearchInput(query=query, collections=collections, limit=limit)
        )

    async def reconcile(self) -> dict[str, int]:
        return await self._backend.reconcile_knowledge()


class Memory:
    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    async def list(
        self,
        *,
        scope: str | None = None,
        kind: MemoryKind | None = None,
        status: str | None = None,
        query: str | None = None,
        limit: int = 200,
    ) -> list[MemoryView]:
        return await self._backend.memories(
            scope=scope, kind=kind, status=status, query=query, limit=limit
        )

    async def scopes(self) -> builtins.list[str]:
        return await self._backend.memory_scopes()

    async def get(self, memory_id: str) -> MemoryDetail:
        return await self._backend.memory(memory_id)

    async def remember(
        self,
        content: str,
        *,
        scope: str,
        kind: MemoryKind = MemoryKind.FACT,
        subject: str | None = None,
        key: str | None = None,
    ) -> WriteResultView:
        spec = RememberInput(scope=scope, content=content, kind=kind, subject=subject, key=key)
        return await self._backend.remember(spec)

    async def retract(self, memory_id: str, *, reason: str) -> MemoryView:
        return await self._backend.retract_memory(memory_id, ForgetInput(reason=reason))

    async def forget(self, memory_id: str, *, reason: str) -> None:
        """Delete the memory's content from the memory store (run logs keep their copies)."""
        await self._backend.forget_memory(memory_id, ForgetInput(reason=reason))

    async def maintain(self) -> MaintenanceView:
        return await self._backend.maintain_memory()


class Tokens:
    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    async def list(self) -> list[TokenView]:
        return await self._backend.tokens()

    async def create(
        self,
        principal: str | None = None,
        *,
        label: str = "",
        expires_in_days: float | None = None,
    ) -> CreatedToken:
        """A new API token (for yourself unless you are an admin naming someone). The value is
        returned only here."""
        owner = principal or (await self._backend.whoami()).name
        spec = TokenInput(principal=owner, label=label, expires_in_days=expires_in_days)
        return await self._backend.create_token(spec)

    async def revoke(self, token_id: str) -> None:
        await self._backend.revoke_token(token_id)


class Principals:
    def __init__(self, backend: Backend) -> None:
        self._backend = backend

    async def list(self) -> list[PrincipalView]:
        return await self._backend.principals()

    async def create(self, spec: PrincipalInput | None = None, /, **fields: Any) -> PrincipalView:
        return await self._backend.create_principal(_spec(PrincipalInput, spec, fields))


class AsyncHollis:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.runs = Runs(backend)
        self.approvals = Approvals(backend)
        self.agents = Agents(backend)
        self.models = Models(backend)
        self.tools = Tools(backend)
        self.knowledge = Knowledge(backend)
        self.memory = Memory(backend)
        self.tokens = Tokens(backend)
        self.principals = Principals(backend)

    @classmethod
    async def open(cls, workspace: str | os.PathLike[str] | None = None) -> Self:
        """The workspace at `workspace` (default: found from the current directory or
        HOLLIS_WORKSPACE), embedded in this process and used as its local admin."""
        from hollis.platform.services import Platform

        root = Path(workspace) if workspace is not None else find_workspace()
        if root is None:
            raise NotFound("no Hollis workspace here; run `hollis init` or set HOLLIS_WORKSPACE")
        platform = await Platform.open(root)
        return cls(LocalBackend(platform, await platform.local_admin()))

    @classmethod
    def connect(cls, url: str, token: str, **options: Any) -> Self:
        """A Hollis server, as the principal `token` belongs to."""
        from hollis.sdk.remote import RemoteBackend

        return cls(RemoteBackend(url, token, **options))

    @classmethod
    async def from_env(cls, workspace: str | os.PathLike[str] | None = None) -> Self:
        """HOLLIS_URL and HOLLIS_TOKEN if set (a server), otherwise the local workspace."""
        url = os.environ.get("HOLLIS_URL")
        if url:
            token = os.environ.get("HOLLIS_TOKEN")
            if not token:
                raise InvalidRequest("HOLLIS_URL is set but HOLLIS_TOKEN is not")
            return cls.connect(url, token)
        return await cls.open(workspace)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        await self.backend.close()

    async def health(self) -> Health:
        return await self.backend.health()

    async def settings(self) -> SettingsView:
        return await self.backend.settings()

    async def update_config(self, config: Mapping[str, Any]) -> SettingsView:
        return await self.backend.update_config(dict(config))

    async def whoami(self) -> PrincipalView:
        return await self.backend.whoami()

    async def audit(self, limit: int = 100) -> list[dict[str, str]]:
        return await self.backend.audit(limit)

    async def login_token(self) -> str:
        """A single-use, ten-minute token to sign in to the web app as yourself."""
        return await self.backend.login_token()


def _uuid(value: UUID | str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(value)
    except ValueError as exc:
        raise InvalidRequest(f"not a run id: {value!r}") from exc


def _spec[M: AgentInput | PrincipalInput](
    model: type[M], spec: M | None, fields: dict[str, Any]
) -> M:
    if spec is not None and fields:
        raise InvalidRequest("give a spec object or keyword fields, not both")
    return spec if spec is not None else model.model_validate(fields)
