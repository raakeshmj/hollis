"""The SDK's HTTP backend: the REST API (`/v1`) with a bearer token (extra `hollis[client]`).

Responses are parsed into the same models the embedded backend returns, and problem details
are raised as the same `PlatformError` subclasses, so code works unchanged against either.
Following a run survives dropped connections: the event stream reconnects with
`Last-Event-ID`, and the run itself never depended on the connection.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from uuid import UUID

from pydantic import TypeAdapter

from hollis import __version__
from hollis.platform.errors import ERRORS, PlatformError, Unavailable
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
from hollis.runtime.knowledge import SourceTrust
from hollis.runtime.memory import MemoryKind

if TYPE_CHECKING:
    import httpx2

LONG_POLL_S = 55.0
STREAM_READ_TIMEOUT_S = 60.0  # the server sends a keep-alive at least every 15 s
RECONNECT_DELAYS_S = (0.5, 1.0, 2.0, 4.0, 8.0)

_principals = TypeAdapter(list[PrincipalView])
_tokens = TypeAdapter(list[TokenView])
_audit = TypeAdapter(list[dict[str, str]])
_deployments = TypeAdapter(list[DeploymentView])
_tools = TypeAdapter(list[ToolView])
_activity = TypeAdapter(list[ToolActivity])
_mcp = TypeAdapter(list[McpServerView])
_agents = TypeAdapter(list[Agent])
_templates = TypeAdapter(list[TemplateView])
_capabilities = TypeAdapter(list[CapabilityView])
_contracts = TypeAdapter(list[ContractView])
_page = TypeAdapter(Page[RunSummary])
_events = TypeAdapter(list[EventView])
_approvals = TypeAdapter(list[Approval])
_collections = TypeAdapter(list[CollectionView])
_sources = TypeAdapter(list[SourceView])
_counts = TypeAdapter(dict[str, int])
_memories = TypeAdapter(list[MemoryView])
_strings = TypeAdapter(list[str])
_strmap = TypeAdapter(dict[str, str])


def _segment(value: str) -> str:
    return quote(value, safe="")


def _path(value: str) -> str:
    return quote(value, safe="/")


class RemoteBackend:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        transport: "httpx2.AsyncBaseTransport | None" = None,
        timeout_s: float = 30.0,
    ) -> None:
        try:
            import httpx2
        except ImportError as exc:  # pragma: no cover - depends on the installed extras
            raise ImportError(
                "connecting to a Hollis server needs the client extra: pip install 'hollis[client]'"
            ) from exc
        self._httpx = httpx2
        self._url = url.rstrip("/")
        self._client = httpx2.AsyncClient(
            base_url=self._url,
            headers={"authorization": f"Bearer {token}", "user-agent": f"hollis-sdk/{__version__}"},
            timeout=timeout_s,
            transport=transport,
        )

    @property
    def location(self) -> str:
        return self._url

    async def close(self) -> None:
        await self._client.aclose()

    # --- plumbing -----------------------------------------------------------------------------

    async def _send(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        timeout_s: float | None = None,
    ) -> "httpx2.Response":
        try:
            response = await self._client.request(
                method,
                path,
                json=body,
                params=params,
                content=content,
                timeout=timeout_s if timeout_s is not None else self._client.timeout,
            )
        except self._httpx.TransportError as exc:
            raise Unavailable(f"cannot reach {self._url}: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise _error(response)
        return response

    async def _json[T](self, adapter: TypeAdapter[T], method: str, path: str, **kw: Any) -> T:
        return adapter.validate_json((await self._send(method, path, **kw)).content)

    async def _model[M](self, model: type[M], method: str, path: str, **kw: Any) -> M:
        return TypeAdapter(model).validate_json((await self._send(method, path, **kw)).content)

    # --- identity and system ------------------------------------------------------------------

    async def whoami(self) -> PrincipalView:
        return await self._model(PrincipalView, "GET", "/v1/me")

    async def principals(self) -> list[PrincipalView]:
        return await self._json(_principals, "GET", "/v1/principals")

    async def create_principal(self, spec: PrincipalInput) -> PrincipalView:
        return await self._model(
            PrincipalView, "POST", "/v1/principals", body=spec.model_dump(mode="json")
        )

    async def tokens(self) -> list[TokenView]:
        return await self._json(_tokens, "GET", "/v1/tokens")

    async def create_token(self, spec: TokenInput) -> CreatedToken:
        return await self._model(
            CreatedToken, "POST", "/v1/tokens", body=spec.model_dump(mode="json")
        )

    async def revoke_token(self, token_id: str) -> None:
        await self._send("DELETE", f"/v1/tokens/{_segment(token_id)}")

    async def audit(self, limit: int) -> list[dict[str, str]]:
        return await self._json(_audit, "GET", "/v1/audit", params={"limit": limit})

    async def health(self) -> Health:
        return await self._model(Health, "GET", "/v1/health")

    async def settings(self) -> SettingsView:
        return await self._model(SettingsView, "GET", "/v1/settings")

    async def update_config(self, config: dict[str, Any]) -> SettingsView:
        return await self._model(SettingsView, "PUT", "/v1/settings/config", body=config)

    # --- models and tools ---------------------------------------------------------------------

    async def deployments(self) -> list[DeploymentView]:
        return await self._json(_deployments, "GET", "/v1/deployments")

    async def deployment(self, deployment_id: str) -> DeploymentView:
        return await self._model(
            DeploymentView, "GET", f"/v1/deployments/{_segment(deployment_id)}"
        )

    async def upsert_deployment(self, spec: DeploymentInput) -> DeploymentView:
        deployment_id = str(spec.deployment.get("id", ""))
        return await self._model(
            DeploymentView,
            "PUT",
            f"/v1/deployments/{_segment(deployment_id)}",
            body=spec.model_dump(mode="json"),
        )

    async def delete_deployment(self, deployment_id: str) -> None:
        await self._send("DELETE", f"/v1/deployments/{_segment(deployment_id)}")

    async def probe_deployment(self, deployment_id: str) -> ProbeResult:
        return await self._model(
            ProbeResult, "POST", f"/v1/deployments/{_segment(deployment_id)}/probe", timeout_s=90
        )

    async def tools(self) -> list[ToolView]:
        return await self._json(_tools, "GET", "/v1/tools")

    async def tool_activity(self, limit: int) -> list[ToolActivity]:
        return await self._json(_activity, "GET", "/v1/tools/activity", params={"limit": limit})

    async def mcp_servers(self) -> list[McpServerView]:
        return await self._json(_mcp, "GET", "/v1/mcp/servers")

    async def connect_mcp(self, name: str) -> McpServerView:
        return await self._model(
            McpServerView, "POST", f"/v1/mcp/servers/{_segment(name)}/connect", timeout_s=90
        )

    # --- agents -------------------------------------------------------------------------------

    async def agents(self) -> list[Agent]:
        return await self._json(_agents, "GET", "/v1/agents")

    async def agent(self, name: str) -> Agent:
        return await self._model(Agent, "GET", f"/v1/agents/{_segment(name)}")

    async def create_agent(self, spec: AgentInput) -> Agent:
        return await self._model(Agent, "POST", "/v1/agents", body=spec.model_dump(mode="json"))

    async def update_agent(self, name: str, spec: AgentInput) -> Agent:
        return await self._model(
            Agent, "PUT", f"/v1/agents/{_segment(name)}", body=spec.model_dump(mode="json")
        )

    async def delete_agent(self, name: str) -> None:
        await self._send("DELETE", f"/v1/agents/{_segment(name)}")

    async def create_agent_from_template(self, spec: FromTemplateInput) -> Agent:
        body = spec.model_dump(mode="json", exclude_unset=True)
        body["overrides"] = spec.overrides.model_dump(mode="json", exclude_unset=True)
        return await self._model(Agent, "POST", "/v1/agents/from-template", body=body)

    async def upgrade_preview(self, name: str) -> UpgradePreview:
        return await self._model(UpgradePreview, "GET", f"/v1/agents/{_segment(name)}/upgrade")

    async def upgrade_agent(self, name: str, spec: UpgradeInput) -> Agent:
        return await self._model(
            Agent,
            "POST",
            f"/v1/agents/{_segment(name)}/upgrade",
            body=spec.model_dump(mode="json"),
        )

    async def export_agent(self, name: str, spec: ExportInput) -> TemplateExport:
        return await self._model(
            TemplateExport,
            "POST",
            f"/v1/agents/{_segment(name)}/export",
            body=spec.model_dump(mode="json", exclude_none=True),
        )

    # --- specialization -----------------------------------------------------------------------

    async def templates(self) -> list[TemplateView]:
        return await self._json(_templates, "GET", "/v1/templates")

    async def template(self, template_id: str) -> TemplateDetail:
        return await self._model(TemplateDetail, "GET", f"/v1/templates/{_segment(template_id)}")

    async def capabilities(self) -> list[CapabilityView]:
        return await self._json(_capabilities, "GET", "/v1/capabilities")

    async def contracts(self) -> list[ContractView]:
        return await self._json(_contracts, "GET", "/v1/contracts")

    async def skills(self) -> SkillsView:
        return await self._model(SkillsView, "GET", "/v1/skills")

    async def create_starter(self) -> Agent:
        return await self._model(Agent, "POST", "/v1/agents/starter")

    async def login_token(self) -> str:
        return (await self._json(_strmap, "POST", "/v1/session/link"))["token"]

    # --- runs and approvals -------------------------------------------------------------------

    async def start_run(self, spec: StartRun) -> RunSummary:
        body = spec.model_dump(mode="json", exclude_none=True)
        return await self._model(RunSummary, "POST", "/v1/runs", body=body)

    async def runs(
        self,
        *,
        phase: RunPhase | None,
        agent: str | None,
        limit: int,
        offset: int,
        roots: bool = False,
    ) -> Page[RunSummary]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if phase is not None:
            params["phase"] = phase.value
        if agent is not None:
            params["agent"] = agent
        if roots:
            params["roots"] = "true"
        return await self._json(_page, "GET", "/v1/runs", params=params)

    async def run(self, run_id: UUID) -> RunDetail:
        return await self._model(RunDetail, "GET", f"/v1/runs/{run_id}")

    async def run_tree(self, run_id: UUID) -> OrchestrationView:
        return await self._model(OrchestrationView, "GET", f"/v1/runs/{run_id}/tree")

    async def run_summary(self, run_id: UUID) -> RunSummary:
        return await self._model(RunSummary, "GET", f"/v1/runs/{run_id}/summary")

    async def wait_run(self, run_id: UUID, timeout_s: float) -> RunSummary:
        """Long polling, in rounds the server bounds, until the run is at rest."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            wait = max(0.0, min(deadline - loop.time(), LONG_POLL_S))
            summary = await self._model(
                RunSummary,
                "GET",
                f"/v1/runs/{run_id}/summary",
                params={"wait_s": round(wait, 3)},
                timeout_s=wait + 30,
            )
            if summary.phase is not RunPhase.RUNNING or loop.time() >= deadline:
                return summary

    async def run_events(self, run_id: UUID, after: int) -> list[EventView]:
        return await self._json(
            _events, "GET", f"/v1/runs/{run_id}/events", params={"after": after}
        )

    async def watch(self, run_id: UUID, after: int) -> AsyncIterator[EventView]:
        last = after
        failures = 0
        timeout = self._httpx.Timeout(
            self._client.timeout.connect or 30, read=STREAM_READ_TIMEOUT_S
        )
        while True:
            try:
                async with self._client.stream(
                    "GET",
                    f"/v1/runs/{run_id}/stream",
                    headers={"last-event-id": str(last)},
                    timeout=timeout,
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        raise _error(response)
                    async for event in self._httpx.EventSource(response):
                        failures = 0
                        if event.event == "end":
                            return
                        if event.event == "run_event":
                            view = EventView.model_validate_json(event.data)
                            last = view.sequence
                            yield view
                # The stream closed without an end event: reconnect from the last event seen.
            except self._httpx.TransportError:
                if failures >= len(RECONNECT_DELAYS_S):
                    raise Unavailable(f"lost the event stream from {self._url}") from None
                await asyncio.sleep(RECONNECT_DELAYS_S[failures])
                failures += 1

    async def cancel_run(self, run_id: UUID, spec: CancelInput) -> RunSummary:
        return await self._model(
            RunSummary, "POST", f"/v1/runs/{run_id}/cancel", body=spec.model_dump(mode="json")
        )

    async def resume_run(self, run_id: UUID) -> RunSummary:
        return await self._model(RunSummary, "POST", f"/v1/runs/{run_id}/resume")

    async def artifact(
        self, run_id: UUID, artifact_id: str, offset: int, length: int
    ) -> ArtifactView:
        return await self._model(
            ArtifactView,
            "GET",
            f"/v1/runs/{run_id}/artifacts/{_segment(artifact_id)}",
            params={"offset": offset, "length": length},
        )

    async def approvals(self) -> list[Approval]:
        return await self._json(_approvals, "GET", "/v1/approvals")

    async def decide(
        self, run_id: UUID, invocation_id: str, decision: ApprovalDecisionInput
    ) -> RunSummary:
        return await self._model(
            RunSummary,
            "POST",
            f"/v1/runs/{run_id}/approvals/{_path(invocation_id)}",
            body=decision.model_dump(mode="json"),
        )

    # --- knowledge ----------------------------------------------------------------------------

    async def collections(self) -> list[CollectionView]:
        return await self._json(_collections, "GET", "/v1/knowledge/collections")

    async def create_collection(self, spec: CollectionInput) -> CollectionView:
        return await self._model(
            CollectionView, "POST", "/v1/knowledge/collections", body=spec.model_dump(mode="json")
        )

    async def delete_collection(self, name: str) -> None:
        await self._send("DELETE", f"/v1/knowledge/collections/{_segment(name)}")

    async def sources(self, collection: str) -> list[SourceView]:
        return await self._json(
            _sources, "GET", f"/v1/knowledge/collections/{_segment(collection)}/sources"
        )

    async def source(self, collection: str, source_id: str) -> SourceDetail:
        return await self._model(
            SourceDetail,
            "GET",
            f"/v1/knowledge/collections/{_segment(collection)}/sources/{_segment(source_id)}",
        )

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
        params: dict[str, Any] = {"labels": list(labels), "trust": trust.value}
        if filename is not None:
            params["filename"] = filename
        return await self._model(
            IngestResultView,
            "PUT",
            f"/v1/knowledge/collections/{_segment(collection)}/documents/{_path(key)}",
            content=data,
            params=params,
            timeout_s=300,
        )

    async def delete_source(self, collection: str, key: str) -> None:
        await self._send(
            "DELETE", f"/v1/knowledge/collections/{_segment(collection)}/documents/{_path(key)}"
        )

    async def relabel(self, collection: str, key: str, labels: tuple[str, ...]) -> SourceView:
        return await self._model(
            SourceView,
            "PATCH",
            f"/v1/knowledge/collections/{_segment(collection)}/documents/{_path(key)}",
            body={"labels": list(labels)},
        )

    async def sync(self, collection: str, spec: SyncInput) -> SyncResultView:
        return await self._model(
            SyncResultView,
            "POST",
            f"/v1/knowledge/collections/{_segment(collection)}/sync",
            body=spec.model_dump(mode="json"),
            timeout_s=600,
        )

    async def search(self, spec: SearchInput) -> SearchResultView:
        return await self._model(
            SearchResultView, "POST", "/v1/knowledge/search", body=spec.model_dump(mode="json")
        )

    async def reconcile_knowledge(self) -> dict[str, int]:
        return await self._json(_counts, "POST", "/v1/knowledge/reconcile", timeout_s=600)

    # --- memory -------------------------------------------------------------------------------

    async def memories(
        self,
        *,
        scope: str | None,
        kind: MemoryKind | None,
        status: str | None,
        query: str | None,
        limit: int,
    ) -> list[MemoryView]:
        params: dict[str, Any] = {"limit": limit}
        for name, value in (("scope", scope), ("status", status), ("query", query)):
            if value is not None:
                params[name] = value
        if kind is not None:
            params["kind"] = kind.value
        return await self._json(_memories, "GET", "/v1/memory", params=params)

    async def memory_scopes(self) -> list[str]:
        return await self._json(_strings, "GET", "/v1/memory/scopes")

    async def memory(self, memory_id: str) -> MemoryDetail:
        return await self._model(MemoryDetail, "GET", f"/v1/memory/{_segment(memory_id)}")

    async def remember(self, spec: RememberInput) -> WriteResultView:
        return await self._model(
            WriteResultView, "POST", "/v1/memory", body=spec.model_dump(mode="json")
        )

    async def retract_memory(self, memory_id: str, spec: ForgetInput) -> MemoryView:
        return await self._model(
            MemoryView,
            "POST",
            f"/v1/memory/{_segment(memory_id)}/retract",
            body=spec.model_dump(mode="json"),
        )

    async def forget_memory(self, memory_id: str, spec: ForgetInput) -> None:
        await self._send(
            "POST", f"/v1/memory/{_segment(memory_id)}/forget", body=spec.model_dump(mode="json")
        )

    async def maintain_memory(self) -> MaintenanceView:
        return await self._model(MaintenanceView, "POST", "/v1/memory/maintenance")


def _error(response: "httpx2.Response") -> PlatformError:
    """The PlatformError a problem-details response describes."""
    try:
        body = json.loads(response.content)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        error = PlatformError(f"HTTP {response.status_code} from the Hollis server")
        error.status = response.status_code
        return error
    problem: dict[str, Any] = body  # pyright: ignore[reportUnknownVariableType]  # parsed JSON
    code = str(problem.get("code", "error"))
    detail = str(problem.get("detail", f"HTTP {response.status_code}"))
    cls = ERRORS.get(code)
    error = cls(detail) if cls is not None else PlatformError(detail)
    if cls is None:
        error.code, error.status = code, response.status_code
        error.title = str(problem.get("title", error.title))
    return error
