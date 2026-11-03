"""SDK backends: the same operations, embedded or over HTTP.

`Backend` is the Platform API with the caller fixed. `LocalBackend` calls an embedded `Platform`
as the workspace's local admin (a process that can open the workspace files can do anything
they allow); `RemoteBackend` (in `remote.py`) calls the REST API with a token, as that token's
principal. Neither contains business logic: authorization and run semantics live in the
platform, behind both.
"""

from collections.abc import AsyncIterator
from typing import Any, Protocol
from uuid import UUID

from hollis.platform.auth import Principal
from hollis.platform.schemas import (
    Agent,
    AgentInput,
    Approval,
    ApprovalDecisionInput,
    ArtifactView,
    CancelInput,
    CapabilityView,
    CollectionInput,
    CollectionView,
    ContractView,
    CreatedToken,
    DeploymentInput,
    DeploymentView,
    EventView,
    ExportInput,
    ForgetInput,
    FromTemplateInput,
    Health,
    IngestResultView,
    MaintenanceView,
    McpServerView,
    MemoryDetail,
    MemoryView,
    OrchestrationView,
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
    SkillsView,
    SourceDetail,
    SourceView,
    StartRun,
    SyncInput,
    SyncResultView,
    TemplateDetail,
    TemplateExport,
    TemplateView,
    TokenInput,
    TokenView,
    ToolActivity,
    ToolView,
    UpgradeInput,
    UpgradePreview,
    WriteResultView,
)
from hollis.platform.services import Platform
from hollis.runtime.knowledge import SourceTrust
from hollis.runtime.memory import MemoryKind


class Backend(Protocol):
    @property
    def location(self) -> str:
        """The workspace path or the server URL."""
        ...

    async def close(self) -> None: ...

    # identity and system
    async def whoami(self) -> PrincipalView: ...
    async def principals(self) -> list[PrincipalView]: ...
    async def create_principal(self, spec: PrincipalInput) -> PrincipalView: ...
    async def tokens(self) -> list[TokenView]: ...
    async def create_token(self, spec: TokenInput) -> CreatedToken: ...
    async def revoke_token(self, token_id: str) -> None: ...
    async def audit(self, limit: int) -> list[dict[str, str]]: ...
    async def health(self) -> Health: ...
    async def settings(self) -> SettingsView: ...
    async def update_config(self, config: dict[str, Any]) -> SettingsView: ...

    # models and tools
    async def deployments(self) -> list[DeploymentView]: ...
    async def deployment(self, deployment_id: str) -> DeploymentView: ...
    async def upsert_deployment(self, spec: DeploymentInput) -> DeploymentView: ...
    async def delete_deployment(self, deployment_id: str) -> None: ...
    async def probe_deployment(self, deployment_id: str) -> ProbeResult: ...
    async def tools(self) -> list[ToolView]: ...
    async def tool_activity(self, limit: int) -> list[ToolActivity]: ...
    async def mcp_servers(self) -> list[McpServerView]: ...
    async def connect_mcp(self, name: str) -> McpServerView: ...

    # agents
    async def agents(self) -> list[Agent]: ...
    async def agent(self, name: str) -> Agent: ...
    async def create_agent(self, spec: AgentInput) -> Agent: ...
    async def update_agent(self, name: str, spec: AgentInput) -> Agent: ...
    async def delete_agent(self, name: str) -> None: ...
    async def create_agent_from_template(self, spec: FromTemplateInput) -> Agent: ...
    async def upgrade_preview(self, name: str) -> UpgradePreview: ...
    async def upgrade_agent(self, name: str, spec: UpgradeInput) -> Agent: ...
    async def export_agent(self, name: str, spec: ExportInput) -> TemplateExport: ...

    # specialization
    async def templates(self) -> list[TemplateView]: ...
    async def template(self, template_id: str) -> TemplateDetail: ...
    async def capabilities(self) -> list[CapabilityView]: ...
    async def contracts(self) -> list[ContractView]: ...
    async def skills(self) -> SkillsView: ...
    async def create_starter(self) -> Agent: ...
    async def login_token(self) -> str: ...

    # runs and approvals
    async def start_run(self, spec: StartRun) -> RunSummary: ...
    async def runs(
        self,
        *,
        phase: RunPhase | None,
        agent: str | None,
        limit: int,
        offset: int,
        roots: bool = False,
    ) -> Page[RunSummary]: ...
    async def run(self, run_id: UUID) -> RunDetail: ...
    async def run_tree(self, run_id: UUID) -> OrchestrationView: ...
    async def run_summary(self, run_id: UUID) -> RunSummary: ...
    async def wait_run(self, run_id: UUID, timeout_s: float) -> RunSummary: ...
    async def run_events(self, run_id: UUID, after: int) -> list[EventView]: ...
    def watch(self, run_id: UUID, after: int) -> AsyncIterator[EventView]: ...
    async def cancel_run(self, run_id: UUID, spec: CancelInput) -> RunSummary: ...
    async def resume_run(self, run_id: UUID) -> RunSummary: ...
    async def artifact(
        self, run_id: UUID, artifact_id: str, offset: int, length: int
    ) -> ArtifactView: ...
    async def approvals(self) -> list[Approval]: ...
    async def decide(
        self, run_id: UUID, invocation_id: str, decision: ApprovalDecisionInput
    ) -> RunSummary: ...

    # knowledge
    async def collections(self) -> list[CollectionView]: ...
    async def create_collection(self, spec: CollectionInput) -> CollectionView: ...
    async def delete_collection(self, name: str) -> None: ...
    async def sources(self, collection: str) -> list[SourceView]: ...
    async def source(self, collection: str, source_id: str) -> SourceDetail: ...
    async def ingest(
        self,
        collection: str,
        key: str,
        data: bytes,
        *,
        filename: str | None,
        labels: tuple[str, ...],
        trust: SourceTrust,
    ) -> IngestResultView: ...
    async def delete_source(self, collection: str, key: str) -> None: ...
    async def relabel(self, collection: str, key: str, labels: tuple[str, ...]) -> SourceView: ...
    async def sync(self, collection: str, spec: SyncInput) -> SyncResultView: ...
    async def search(self, spec: SearchInput) -> SearchResultView: ...
    async def reconcile_knowledge(self) -> dict[str, int]: ...

    # memory
    async def memories(
        self,
        *,
        scope: str | None,
        kind: MemoryKind | None,
        status: str | None,
        query: str | None,
        limit: int,
    ) -> list[MemoryView]: ...
    async def memory_scopes(self) -> list[str]: ...
    async def memory(self, memory_id: str) -> MemoryDetail: ...
    async def remember(self, spec: RememberInput) -> WriteResultView: ...
    async def retract_memory(self, memory_id: str, spec: ForgetInput) -> MemoryView: ...
    async def forget_memory(self, memory_id: str, spec: ForgetInput) -> None: ...
    async def maintain_memory(self) -> MaintenanceView: ...


class LocalBackend:
    """An embedded workspace, used as its local admin."""

    def __init__(self, platform: Platform, principal: Principal, *, owned: bool = True) -> None:
        self.platform = platform
        self._me = principal
        self._owned = owned

    @property
    def location(self) -> str:
        return str(self.platform.ws.root)

    async def close(self) -> None:
        if self._owned:
            await self.platform.close()

    async def whoami(self) -> PrincipalView:
        return await self.platform.whoami(self._me)

    async def principals(self) -> list[PrincipalView]:
        return await self.platform.principals(self._me)

    async def create_principal(self, spec: PrincipalInput) -> PrincipalView:
        return await self.platform.create_principal(self._me, spec)

    async def tokens(self) -> list[TokenView]:
        return await self.platform.tokens(self._me)

    async def create_token(self, spec: TokenInput) -> CreatedToken:
        return await self.platform.create_token(self._me, spec)

    async def revoke_token(self, token_id: str) -> None:
        await self.platform.revoke_token(self._me, token_id)

    async def audit(self, limit: int) -> list[dict[str, str]]:
        return await self.platform.audit(self._me, limit)

    async def health(self) -> Health:
        return await self.platform.health(self._me)

    async def settings(self) -> SettingsView:
        return await self.platform.settings(self._me)

    async def update_config(self, config: dict[str, Any]) -> SettingsView:
        return await self.platform.update_config(self._me, config)

    async def deployments(self) -> list[DeploymentView]:
        return await self.platform.deployments(self._me)

    async def deployment(self, deployment_id: str) -> DeploymentView:
        return await self.platform.deployment(self._me, deployment_id)

    async def upsert_deployment(self, spec: DeploymentInput) -> DeploymentView:
        return await self.platform.upsert_deployment(self._me, spec)

    async def delete_deployment(self, deployment_id: str) -> None:
        await self.platform.delete_deployment(self._me, deployment_id)

    async def probe_deployment(self, deployment_id: str) -> ProbeResult:
        return await self.platform.probe_deployment(self._me, deployment_id)

    async def tools(self) -> list[ToolView]:
        return await self.platform.tools(self._me)

    async def tool_activity(self, limit: int) -> list[ToolActivity]:
        return await self.platform.tool_activity(self._me, limit)

    async def mcp_servers(self) -> list[McpServerView]:
        return await self.platform.mcp_servers(self._me)

    async def connect_mcp(self, name: str) -> McpServerView:
        return await self.platform.connect_mcp(self._me, name)

    async def agents(self) -> list[Agent]:
        return await self.platform.agents(self._me)

    async def agent(self, name: str) -> Agent:
        return await self.platform.agent(self._me, name)

    async def create_agent(self, spec: AgentInput) -> Agent:
        return await self.platform.create_agent(self._me, spec)

    async def update_agent(self, name: str, spec: AgentInput) -> Agent:
        return await self.platform.update_agent(self._me, name, spec)

    async def delete_agent(self, name: str) -> None:
        await self.platform.delete_agent(self._me, name)

    async def create_agent_from_template(self, spec: FromTemplateInput) -> Agent:
        return await self.platform.create_agent_from_template(self._me, spec)

    async def upgrade_preview(self, name: str) -> UpgradePreview:
        return await self.platform.upgrade_preview(self._me, name)

    async def upgrade_agent(self, name: str, spec: UpgradeInput) -> Agent:
        return await self.platform.upgrade_agent(self._me, name, spec)

    async def export_agent(self, name: str, spec: ExportInput) -> TemplateExport:
        return await self.platform.export_agent(self._me, name, spec)

    async def templates(self) -> list[TemplateView]:
        return await self.platform.templates(self._me)

    async def template(self, template_id: str) -> TemplateDetail:
        return await self.platform.template(self._me, template_id)

    async def capabilities(self) -> list[CapabilityView]:
        return await self.platform.capabilities(self._me)

    async def contracts(self) -> list[ContractView]:
        return await self.platform.contracts(self._me)

    async def skills(self) -> SkillsView:
        return await self.platform.skills(self._me)

    async def create_starter(self) -> Agent:
        return await self.platform.create_starter(self._me)

    async def login_token(self) -> str:
        return await self.platform.login_link_token(self._me)

    async def start_run(self, spec: StartRun) -> RunSummary:
        return await self.platform.start_run(self._me, spec)

    async def runs(
        self,
        *,
        phase: RunPhase | None,
        agent: str | None,
        limit: int,
        offset: int,
        roots: bool = False,
    ) -> Page[RunSummary]:
        return await self.platform.runs(
            self._me, phase=phase, agent=agent, roots=roots, limit=limit, offset=offset
        )

    async def run(self, run_id: UUID) -> RunDetail:
        return await self.platform.run(self._me, run_id)

    async def run_tree(self, run_id: UUID) -> OrchestrationView:
        return await self.platform.run_tree(self._me, run_id)

    async def run_summary(self, run_id: UUID) -> RunSummary:
        return await self.platform.run_summary(self._me, run_id)

    async def wait_run(self, run_id: UUID, timeout_s: float) -> RunSummary:
        return await self.platform.wait_run(self._me, run_id, timeout_s=timeout_s)

    async def run_events(self, run_id: UUID, after: int) -> list[EventView]:
        return await self.platform.run_events(self._me, run_id, after)

    async def watch(self, run_id: UUID, after: int) -> AsyncIterator[EventView]:
        async for event in self.platform.watch(self._me, run_id, after):
            if event is not None:  # None marks an idle interval (a keep-alive over HTTP)
                yield event

    async def cancel_run(self, run_id: UUID, spec: CancelInput) -> RunSummary:
        return await self.platform.cancel_run(self._me, run_id, spec)

    async def resume_run(self, run_id: UUID) -> RunSummary:
        return await self.platform.resume_run(self._me, run_id)

    async def artifact(
        self, run_id: UUID, artifact_id: str, offset: int, length: int
    ) -> ArtifactView:
        return await self.platform.artifact(
            self._me, run_id, artifact_id, offset=offset, length=length
        )

    async def approvals(self) -> list[Approval]:
        return await self.platform.approvals(self._me)

    async def decide(
        self, run_id: UUID, invocation_id: str, decision: ApprovalDecisionInput
    ) -> RunSummary:
        return await self.platform.decide(self._me, run_id, invocation_id, decision)

    async def collections(self) -> list[CollectionView]:
        return await self.platform.collections(self._me)

    async def create_collection(self, spec: CollectionInput) -> CollectionView:
        return await self.platform.create_collection(self._me, spec)

    async def delete_collection(self, name: str) -> None:
        await self.platform.delete_collection(self._me, name)

    async def sources(self, collection: str) -> list[SourceView]:
        return await self.platform.sources(self._me, collection)

    async def source(self, collection: str, source_id: str) -> SourceDetail:
        return await self.platform.source(self._me, collection, source_id)

    async def ingest(
        self,
        collection: str,
        key: str,
        data: bytes,
        *,
        filename: str | None,
        labels: tuple[str, ...],
        trust: SourceTrust,
    ) -> IngestResultView:
        return await self.platform.ingest(
            self._me, collection, key=key, data=data, filename=filename, labels=labels, trust=trust
        )

    async def delete_source(self, collection: str, key: str) -> None:
        await self.platform.delete_source(self._me, collection, key)

    async def relabel(self, collection: str, key: str, labels: tuple[str, ...]) -> SourceView:
        return await self.platform.relabel(self._me, collection, key, labels)

    async def sync(self, collection: str, spec: SyncInput) -> SyncResultView:
        return await self.platform.sync(self._me, collection, spec)

    async def search(self, spec: SearchInput) -> SearchResultView:
        return await self.platform.search(self._me, spec)

    async def reconcile_knowledge(self) -> dict[str, int]:
        return await self.platform.reconcile_knowledge(self._me)

    async def memories(
        self,
        *,
        scope: str | None,
        kind: MemoryKind | None,
        status: str | None,
        query: str | None,
        limit: int,
    ) -> list[MemoryView]:
        return await self.platform.memories(
            self._me, scope=scope, kind=kind, status=status, query=query, limit=limit
        )

    async def memory_scopes(self) -> list[str]:
        return await self.platform.memory_scopes(self._me)

    async def memory(self, memory_id: str) -> MemoryDetail:
        return await self.platform.memory(self._me, memory_id)

    async def remember(self, spec: RememberInput) -> WriteResultView:
        return await self.platform.remember(self._me, spec)

    async def retract_memory(self, memory_id: str, spec: ForgetInput) -> MemoryView:
        return await self.platform.retract_memory(self._me, memory_id, spec)

    async def forget_memory(self, memory_id: str, spec: ForgetInput) -> None:
        await self.platform.forget_memory(self._me, memory_id, spec)

    async def maintain_memory(self) -> MaintenanceView:
        return await self.platform.maintain_memory(self._me)
