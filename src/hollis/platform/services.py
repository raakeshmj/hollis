"""The Hollis Platform API: the single implementation behind the REST API, the SDK and the CLI.

Every method takes the calling `Principal` and enforces authorization here (ADR-0028):
- admins can do everything; operators run agents and manage their own runs and memory; viewers
  read;
- runs are visible to their creator and admins (or to everyone, with run_visibility=workspace);
- approving needs the approve permission *and* access to the run, and goes through
  `Runner.resolve_approval`, so binding and expiry checks are the runtime's (ADR-0015);
- memory and knowledge views are filtered by the principal's grants;
- a run's effective memory and knowledge access is the agent's configuration intersected with
  the caller's grants: running a broadly configured agent never widens what a caller can reach.
"""

import os
import time
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError

from hollis import __version__
from hollis.adapters.config import BearerAuth, HeaderAuth
from hollis.adapters.credentials import MissingCredential
from hollis.adapters.demo_model import DEMO_DEPLOYMENT
from hollis.gateway.catalog import AnyDeployment, Deployment, OpenAIEmbeddingsDeployment
from hollis.gateway.gateway import RoutingDenied, WrongTask
from hollis.knowledge.model import Chunk, Source
from hollis.memory import ChangeKind, MemoryCandidate, MemoryRecord, Provenance, SourceKind
from hollis.platform.auth import Principal
from hollis.platform.config import WorkspaceConfig
from hollis.platform.errors import Conflict, Forbidden, InvalidRequest, NotFound, Unavailable
from hollis.platform.schemas import (
    Agent,
    AgentInput,
    Approval,
    ApprovalDecisionInput,
    ArtifactView,
    CancelInput,
    ChunkView,
    CollectionInput,
    CollectionView,
    CreatedToken,
    CredentialView,
    DeploymentInput,
    DeploymentView,
    EventView,
    ForgetInput,
    Health,
    IngestResultView,
    MaintenanceView,
    McpServerView,
    MemoryChangeView,
    MemoryDetail,
    MemoryView,
    Page,
    PrincipalInput,
    PrincipalView,
    ProbeResult,
    RememberInput,
    Role,
    RunDetail,
    RunPhase,
    RunSummary,
    SearchHit,
    SearchInput,
    SearchResultView,
    SectionView,
    SettingsView,
    SourceDetail,
    SourceView,
    StartRun,
    StorageView,
    SyncInput,
    SyncResultView,
    TokenInput,
    TokenView,
    ToolActivity,
    ToolView,
    UsageView,
    WriteResultView,
)
from hollis.platform.views import TERMINAL_STATUSES, detail, event_views, run_phase
from hollis.platform.workspace import LOCAL_EMBEDDER, Workspace
from hollis.runtime.context import ContextPolicy
from hollis.runtime.embedding import EmbeddingRequest, InputType
from hollis.runtime.knowledge import KnowledgeAccess, KnowledgeQuery, SourceTrust
from hollis.runtime.memory import Certainty, MemoryAccess, MemoryKind
from hollis.runtime.model import (
    CapabilityMismatch,
    ModelCallError,
    ModelRequest,
    UnknownModel,
)
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import RunSpec, SecretInRunInput, UnknownTool
from hollis.runtime.state import fold
from hollis.runtime.store import load_run
from hollis.runtime.tools import ApprovalDecision, PolicyRequest

STARTER_AGENT = "assistant"
STARTER_COLLECTION = "docs"
STARTER_SCOPE = "workspace:main"
STARTER_TOOLS = (
    "fs_list_dir",
    "fs_read_file",
    "fs_write_file",
    "knowledge_search",
    "knowledge_read",
    "memory_recall",
    "memory_remember",
)
STARTER_INSTRUCTIONS = (
    "You are a careful assistant for this workspace. Use tools when they help, cite knowledge "
    "passages by their keys, and say when you are unsure."
)


class Platform:
    def __init__(self, workspace: Workspace) -> None:
        self.ws = workspace

    @classmethod
    async def open(cls, root: Path) -> "Platform":
        return cls(await Workspace.open(root))

    async def close(self) -> None:
        await self.ws.close()

    # --- identity ------------------------------------------------------------------------------

    async def local_admin(self) -> Principal:
        """The principal for embedded use (the process can read the workspace files anyway)."""
        return await self.ws.auth.ensure_admin()

    async def authenticate(self, token: str) -> Principal:
        return await self.ws.auth.authenticate(token)

    async def whoami(self, p: Principal) -> PrincipalView:
        return p.view()

    async def principals(self, p: Principal) -> list[PrincipalView]:
        _admin(p)
        return [x.view() for x in await self.ws.auth.principals()]

    async def create_principal(self, p: Principal, spec: PrincipalInput) -> PrincipalView:
        _admin(p)
        if await self.ws.auth.principal(spec.name) is not None:
            raise Conflict(f"principal {spec.name!r} already exists")
        created = await self.ws.auth.create_principal(spec)
        await self.ws.db.audit(p.name, "principal.create", spec.name, spec.role.value)
        return created.view()

    async def tokens(self, p: Principal) -> list[TokenView]:
        return await self.ws.auth.tokens(None if p.is_admin else p.name)

    async def create_token(self, p: Principal, spec: TokenInput) -> CreatedToken:
        if not p.is_admin and spec.principal != p.name:
            raise Forbidden("only admins can create tokens for other principals")
        token = await self.ws.auth.create_token(
            spec.principal, label=spec.label, expires_in_days=spec.expires_in_days
        )
        await self.ws.db.audit(p.name, "token.create", token.token_id, spec.principal)
        return token

    async def revoke_token(self, p: Principal, token_id: str) -> None:
        owned = {t.token_id for t in await self.ws.auth.tokens(None if p.is_admin else p.name)}
        if token_id not in owned or not await self.ws.auth.revoke(token_id):
            raise NotFound(f"no active token {token_id!r}")
        await self.ws.db.audit(p.name, "token.revoke", token_id)

    async def login_token(self, principal: str = "admin") -> str:
        """A single-use, ten-minute token for the web app's login link (`hollis serve`)."""
        await self.ws.auth.ensure_admin()
        return (await self.ws.auth.create_token(principal, label="login link", kind="login")).token

    async def login_link_token(self, p: Principal) -> str:
        """A single-use, ten-minute sign-in token for the caller (to open the web app)."""
        token = await self.ws.auth.create_token(p.name, label="login link", kind="login")
        await self.ws.db.audit(p.name, "token.login_link", token.token_id)
        return token.token

    async def audit(self, p: Principal, limit: int = 100) -> list[dict[str, str]]:
        _admin(p)
        return await self.ws.db.audit_log(limit)

    # --- health and settings -------------------------------------------------------------------

    async def health(self, p: Principal) -> Health:
        deployments = await self.deployments(p)
        problems = [f"{d.id}: {d.status_detail}" for d in deployments if d.status != "ready"]
        problems += [
            f"MCP server {c.config.name}: {c.error}"
            for c in self.ws.mcp.values()
            if c.status == "error"
        ]
        waiting = (await self.ws.db.query("SELECT COUNT(*) FROM run_index WHERE awaiting > 0"))[0][
            0
        ]
        storage = tuple(
            StorageView(
                name=name, path=str(path), bytes=path.stat().st_size if path.exists() else 0
            )
            for name, path in self.ws.storage()
        )
        return Health(
            status="degraded" if problems else "ok",
            version=__version__,
            workspace=self.ws.config.workspace.name,
            workspace_path=str(self.ws.root),
            environment=self.ws.config.runtime.environment,
            demo_model=self.ws.config.demo,
            storage=storage,
            deployments=len(deployments),
            deployment_problems=sum(1 for d in deployments if d.status != "ready"),
            active_runs=self.ws.supervisor.active_count,
            pending_approvals=int(waiting),
            problems=tuple(problems),
        )

    async def settings(self, p: Principal) -> SettingsView:
        config = self.ws.config
        return SettingsView(
            workspace=config.workspace.name,
            workspace_path=str(self.ws.root),
            config_path=str(self.ws.root / "hollis.toml"),
            config=config.model_dump(mode="json"),
            server=config.server.model_dump(mode="json"),
            auth={
                "principals": len(await self.ws.auth.principals()) if p.is_admin else None,
                "you": p.name,
                "role": p.role.value,
                "authentication": "required (tokens and sessions)",
            },
        )

    async def update_config(self, p: Principal, config: dict[str, Any]) -> SettingsView:
        _admin(p)
        try:
            parsed = WorkspaceConfig.model_validate(config)
        except ValidationError as exc:
            raise InvalidRequest(
                f"invalid configuration: {exc.error_count()} errors: {_first_error(exc)}"
            ) from exc
        await self.ws.apply_config(parsed)
        await self.ws.db.audit(p.name, "config.update", "hollis.toml")
        return await self.settings(p)

    async def patch_config(self, p: Principal, changes: dict[str, Any]) -> SettingsView:
        """Merge `changes` into the settings hollis.toml sets (admin), so the file keeps only
        what someone chose, then validate and apply like update_config."""
        _admin(p)
        current = self.ws.config.model_dump(mode="json", exclude_unset=True)
        return await self.update_config(p, _merge(current, changes))

    # --- models ---------------------------------------------------------------------------------

    async def deployments(self, p: Principal) -> list[DeploymentView]:
        views: list[DeploymentView] = []
        demo = self.ws.resolver.demo
        if demo is not None:
            d = demo.descriptor
            views.append(
                DeploymentView(
                    id=d.deployment,
                    provider=d.provider,
                    protocol=d.protocol,
                    model=d.model,
                    endpoint=None,
                    locality=d.locality.value,
                    tasks=("text_generation",),
                    capabilities=d.capabilities.model_dump(mode="json"),
                    builtin=True,
                    description=(
                        "Deterministic rules for trying Hollis without a model. "
                        "Not a language model."
                    ),
                )
            )
        if self.ws.config.knowledge.embedder == LOCAL_EMBEDDER:
            e = self.ws.embedder.descriptor
            views.append(
                DeploymentView(
                    id=LOCAL_EMBEDDER,
                    provider="hollis",
                    protocol="local-hashing",
                    model=e.model,
                    endpoint=None,
                    locality="local",
                    tasks=("embedding",),
                    capabilities=e.capabilities.model_dump(mode="json"),
                    builtin=True,
                    description=(
                        "Character n-gram hashing embedder: surface similarity "
                        "(identifier parts, spelling), not meaning."
                    ),
                )
            )
        allowed = self.ws.config.routing.allowed_localities
        for deployment in self.ws.config.deployments:
            views.append(self._deployment_view(deployment, allowed))
        return views

    def _deployment_view(
        self, deployment: AnyDeployment, allowed: frozenset[Any]
    ) -> DeploymentView:
        auth = deployment.endpoint.auth
        credential = None
        status: Any = "ready"
        detail_text = ""
        if isinstance(auth, BearerAuth | HeaderAuth):
            present = bool(os.environ.get(auth.env))
            credential = CredentialView(env=auth.env, present=present)
            if not present:
                status, detail_text = (
                    "missing_credential",
                    f"environment variable {auth.env} is not set",
                )
        if deployment.locality not in allowed:
            status, detail_text = (
                "blocked_by_routing",
                f"routing policy does not allow {deployment.locality.value} deployments",
            )
        caps = deployment.capabilities
        return DeploymentView(
            id=deployment.id,
            provider=deployment.provider,
            protocol=deployment.protocol,
            model=deployment.model,
            endpoint=deployment.endpoint.origin,
            locality=deployment.locality.value,
            tasks=tuple(sorted(t.value for t in caps.tasks)),
            capabilities=caps.model_dump(mode="json"),
            credential=credential,
            status=status,
            status_detail=detail_text,
        )

    async def deployment(self, p: Principal, deployment_id: str) -> DeploymentView:
        for view in await self.deployments(p):
            if view.id == deployment_id:
                return view
        raise NotFound(f"no deployment {deployment_id!r}")

    async def upsert_deployment(self, p: Principal, spec: DeploymentInput) -> DeploymentView:
        _admin(p)
        try:
            parsed: AnyDeployment = TypeAdapter(Deployment).validate_python(spec.deployment)
        except ValidationError as exc:
            raise InvalidRequest(f"invalid deployment: {_first_error(exc)}") from exc
        if parsed.id in (DEMO_DEPLOYMENT, LOCAL_EMBEDDER):
            raise Conflict(f"{parsed.id!r} is a built-in deployment name")
        others = tuple(d for d in self.ws.config.deployments if d.id != parsed.id)
        config = self.ws.config.model_copy(update={"deployments": (*others, parsed)})
        await self.ws.apply_config(config)
        await self.ws.db.audit(p.name, "deployment.upsert", parsed.id, parsed.protocol)
        return await self.deployment(p, parsed.id)

    async def delete_deployment(self, p: Principal, deployment_id: str) -> None:
        _admin(p)
        if deployment_id not in {d.id for d in self.ws.config.deployments}:
            raise NotFound(f"no configured deployment {deployment_id!r}")
        users = [a.name for a in await self.agents(p) if a.model == deployment_id]
        if users:
            raise Conflict(f"agents use this deployment: {', '.join(users)}")
        if self.ws.config.knowledge.embedder == deployment_id:
            raise Conflict("the knowledge embedder uses this deployment")
        config = self.ws.config.model_copy(
            update={
                "deployments": tuple(d for d in self.ws.config.deployments if d.id != deployment_id)
            }
        )
        await self.ws.apply_config(config)
        await self.ws.db.audit(p.name, "deployment.delete", deployment_id)

    async def probe_deployment(self, p: Principal, deployment_id: str) -> ProbeResult:
        """Send one tiny request to a deployment. An explicit, opt-in network call."""
        _operator(p)
        view = await self.deployment(p, deployment_id)
        started = time.perf_counter()
        try:
            if "embedding" in view.tasks:
                embedder = (
                    self.ws.embedder
                    if deployment_id == LOCAL_EMBEDDER
                    else self.ws.gateway.embedder(deployment_id)
                )
                response = await embedder.embed(
                    EmbeddingRequest(inputs=("ping",), input_type=InputType.QUERY, timeout_s=20)
                )
                served, text = (
                    response.served_model,
                    f"{len(response.vectors[0])}-dimensional vector",
                )
            else:
                provider = self.ws.resolver.resolve(deployment_id)
                reply = await provider.generate(
                    ModelRequest(
                        operation_id=f"probe/{uuid4()}",
                        model=provider.descriptor.model,
                        input="Reply with the single word: ok",
                        max_output_tokens=16,
                        timeout_s=30,
                    )
                )
                served, text = reply.served_model, reply.content[:200]
        except ModelCallError as exc:
            return ProbeResult(
                deployment=deployment_id,
                ok=False,
                detail=f"{exc.kind.value}: {self.ws.redactor.redact(exc.message)}",
            )
        except (UnknownModel, RoutingDenied, WrongTask, MissingCredential, ValueError) as exc:
            return ProbeResult(
                deployment=deployment_id, ok=False, detail=self.ws.redactor.redact(str(exc))
            )
        return ProbeResult(
            deployment=deployment_id,
            ok=True,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            detail=text,
            served_model=served,
        )

    # --- tools ----------------------------------------------------------------------------------

    async def tools(self, p: Principal) -> list[ToolView]:
        views: list[ToolView] = []
        for tool in self.ws.registry:
            d = tool.descriptor
            verdict = self.ws.policy.decide(
                PolicyRequest(
                    run_id=uuid4(),
                    invocation_id="preview",
                    tool=d.ref,
                    arguments={},
                    environment=self.ws.config.runtime.environment,
                )
            )
            views.append(
                ToolView(
                    name=d.name,
                    description=d.description,
                    version=d.version,
                    effect=d.effect.value,
                    trust=d.trust.value,
                    idempotent=d.idempotent,
                    reconcilable=d.reconcilable,
                    isolation=d.isolation.value,
                    timeout_s=d.timeout_s,
                    input_schema=d.input_schema,
                    source=self.ws.tool_sources.get(d.name, "builtin"),
                    policy=verdict.decision.value,
                    policy_rule=verdict.rule,
                    policy_reason=verdict.reason,
                )
            )
        return sorted(views, key=lambda v: (v.source, v.name))

    async def mcp_servers(self, p: Principal) -> list[McpServerView]:
        return [self._mcp_view(name) for name in self.ws.mcp]

    def _mcp_view(self, name: str) -> McpServerView:
        connection = self.ws.mcp[name]
        config = connection.config
        transport = config.transport
        target = transport.describe() if transport.type == "stdio" else transport.endpoint.origin  # type: ignore[union-attr]  # discriminated by type
        server = connection.server
        return McpServerView(
            name=name,
            transport=transport.type,
            target=target,
            allowlist=tuple(sorted(config.tools)),
            status=connection.status,  # type: ignore[arg-type]  # one of the declared states
            tools=tuple(sorted(server.tools)) if server is not None else (),
            ignored=tuple(server.ignored) if server is not None else (),
            unavailable=tuple(f"{k}: {v}" for k, v in server.unavailable.items())
            if server is not None
            else (),
            error=connection.error,
            connected_at=connection.connected_at,
        )

    async def connect_mcp(self, p: Principal, name: str) -> McpServerView:
        _admin(p)
        if name not in self.ws.mcp:
            raise NotFound(f"no MCP server {name!r} in the configuration")
        await self.ws.connect_mcp(name)
        await self.ws.db.audit(p.name, "mcp.connect", name, self.ws.mcp[name].status)
        return self._mcp_view(name)

    async def tool_activity(self, p: Principal, limit: int = 50) -> list[ToolActivity]:
        rows, _ = await self.ws.db.index_list(owner=self._owner_filter(p), limit=30)
        activity: list[ToolActivity] = []
        for row in rows:
            if row["data"].get("tool_calls", 0) == 0:
                continue
            state = await load_run(self.ws.runs, row["run_id"])
            for turn in state.turns:
                for inv in turn.invocations:
                    activity.append(
                        ToolActivity(
                            run_id=state.run_id,
                            agent=row["agent"],
                            invocation_id=inv.invocation_id,
                            tool=inv.call.name,
                            decision=inv.decision.value if inv.decision else None,
                            outcome=inv.outcome.value if inv.outcome else None,
                            approver=inv.approver,
                            at=inv.decided_at or row["updated_at"],
                        )
                    )
        activity.sort(key=lambda a: a.at, reverse=True)
        return activity[:limit]

    # --- agents ---------------------------------------------------------------------------------

    async def agents(self, p: Principal) -> list[Agent]:
        rows = await self.ws.db.query("SELECT data FROM agents ORDER BY name")
        return [Agent.model_validate_json(r[0]) for r in rows]

    async def agent(self, p: Principal, name: str) -> Agent:
        rows = await self.ws.db.query("SELECT data FROM agents WHERE name = ?", (name,))
        if not rows:
            raise NotFound(f"no agent {name!r}")
        return Agent.model_validate_json(rows[0][0])

    async def create_agent(self, p: Principal, spec: AgentInput) -> Agent:
        _admin(p)
        if await self.ws.db.query("SELECT 1 FROM agents WHERE name = ?", (spec.name,)):
            raise Conflict(f"agent {spec.name!r} already exists")
        await self._validate_agent(spec)
        now = datetime.now(UTC)
        agent = Agent(
            **spec.model_dump(), created_at=now, updated_at=now, created_by=p.name, version=1
        )
        await self.ws.db.execute(
            "INSERT INTO agents (name, data, version) VALUES (?, ?, 1)",
            (agent.name, agent.model_dump_json()),
        )
        await self.ws.db.audit(p.name, "agent.create", agent.name)
        return agent

    async def update_agent(self, p: Principal, name: str, spec: AgentInput) -> Agent:
        _admin(p)
        current = await self.agent(p, name)
        if spec.name != name:
            raise InvalidRequest("an agent cannot be renamed; create a new one")
        await self._validate_agent(spec)
        agent = Agent(
            **spec.model_dump(),
            created_at=current.created_at,
            updated_at=datetime.now(UTC),
            created_by=current.created_by,
            version=current.version + 1,
        )
        await self.ws.db.execute(
            "UPDATE agents SET data = ?, version = ? WHERE name = ?",
            (agent.model_dump_json(), agent.version, name),
        )
        await self.ws.db.audit(p.name, "agent.update", name, f"version {agent.version}")
        return agent

    async def delete_agent(self, p: Principal, name: str) -> None:
        _admin(p)
        if not await self.ws.db.execute("DELETE FROM agents WHERE name = ?", (name,)):
            raise NotFound(f"no agent {name!r}")
        await self.ws.db.audit(p.name, "agent.delete", name)

    async def create_starter(self, p: Principal) -> Agent:
        """The starter setup `hollis init` offers: a `docs` collection and an `assistant` agent
        on the default (demo) model with the built-in tools this workspace has. Idempotent."""
        _admin(p)
        if await self.ws.knowledge_store.collection(STARTER_COLLECTION) is None:
            await self.create_collection(
                p, CollectionInput(name=STARTER_COLLECTION, description="Workspace documents")
            )
        name = self.ws.config.runtime.default_agent or STARTER_AGENT
        if await self.ws.db.query("SELECT 1 FROM agents WHERE name = ?", (name,)):
            return await self.agent(p, name)
        tools = tuple(t for t in STARTER_TOOLS if self.ws.registry.get(t) is not None)
        return await self.create_agent(
            p,
            AgentInput(
                name=name,
                description="Starter agent. Point it at a real model once you add a deployment.",
                instructions=STARTER_INSTRUCTIONS,
                model=DEMO_DEPLOYMENT if self.ws.resolver.demo is not None else "",
                tools=tools,
                memory=MemoryAccess(read=(STARTER_SCOPE,), write=(STARTER_SCOPE,)),
                knowledge=KnowledgeAccess(collections=(STARTER_COLLECTION,)),
            ),
        )

    async def _validate_agent(self, spec: AgentInput) -> None:
        self._check_model(spec.model)
        unknown = [
            t for t in spec.tools if self.ws.registry.get(t) is None and not self._mcp_tool(t)
        ]
        if unknown:
            raise InvalidRequest(f"unknown tools: {', '.join(unknown)}")

    def _check_model(self, model: str) -> None:
        """The model must be a configured text deployment the routing policy allows. Credentials
        are not needed yet: they are read when a run uses the deployment."""
        if model == DEMO_DEPLOYMENT and self.ws.resolver.demo is not None:
            return
        deployment = self.ws.gateway.catalog.get(model)
        if deployment is None:
            raise InvalidRequest(f"model: no deployment {model!r}")
        if isinstance(deployment, OpenAIEmbeddingsDeployment):
            raise InvalidRequest(f"model: {model!r} is an embedding deployment")
        if deployment.locality not in self.ws.config.routing.allowed_localities:
            raise InvalidRequest(
                f"model: routing policy does not allow {deployment.locality.value} deployments"
            )

    def _mcp_tool(self, name: str) -> bool:
        return self._mcp_server(name) is not None

    def _mcp_server(self, tool: str) -> str | None:
        """The configured MCP server a tool name belongs to (tools are named mcp_<server>_*)."""
        matches = [s for s in self.ws.mcp if tool.startswith(f"mcp_{s}_")]
        return max(matches, key=len) if matches else None

    # --- runs -----------------------------------------------------------------------------------

    async def start_run(self, p: Principal, spec: StartRun) -> RunSummary:
        """Agents are the unit of delegation: operators run agents an admin defined; ad-hoc
        runs (a model, tools and instructions given directly) are for admins."""
        _operator(p)
        record_episodes = False
        default = self.ws.config.runtime.default_agent
        if spec.agent is None and spec.model is None and default is not None:
            spec = spec.model_copy(update={"agent": default})
        if spec.agent is not None:
            if spec.model is not None or spec.tools or spec.instructions is not None:
                raise InvalidRequest("give an agent, or a model with tools and instructions")
            agent = await self.agent(p, spec.agent)
            model, instructions, tools = agent.model, agent.instructions or None, agent.tools
            budget = agent.budget or RunBudget(
                max_output_tokens=self.ws.config.runtime.max_output_tokens
            )
            context, memory, knowledge = agent.context, agent.memory, agent.knowledge
            record_episodes = agent.record_episodes
        elif spec.model is not None:
            _admin(p)
            model, instructions, tools = spec.model, spec.instructions, spec.tools
            budget = RunBudget(max_output_tokens=self.ws.config.runtime.max_output_tokens)
            context, memory, knowledge = ContextPolicy(), None, None
        else:
            raise InvalidRequest("give an agent or a model (no default agent is configured)")
        memory = _effective_memory(memory, p)
        if spec.session is not None:  # a conversation scope private to this principal
            scope = f"session:{p.name}/{spec.session}"
            read = (*(memory.read if memory else ()), scope)
            write = (*(memory.write if memory else ()), scope)
            memory = MemoryAccess(
                read=tuple(dict.fromkeys(read)), write=tuple(dict.fromkeys(write))
            )
        for tool in tools:
            server = self._mcp_server(tool)
            if server is not None and self.ws.registry.get(tool) is None:
                await self.ws.connect_mcp(server)
        try:
            state = await self.ws.runner.create(
                RunSpec(
                    model=model,
                    input=spec.input,
                    instructions=instructions,
                    budget=budget,
                    tools=tools,
                    context=context,
                    memory=memory,
                    knowledge=_effective_knowledge(knowledge, p),
                )
            )
        except (UnknownModel, RoutingDenied, WrongTask, CapabilityMismatch, UnknownTool) as exc:
            raise InvalidRequest(str(exc)) from exc
        except SecretInRunInput as exc:
            raise InvalidRequest(str(exc)) from exc
        except MissingCredential as exc:
            raise Unavailable(f"model {model!r} cannot be used: {exc}") from exc
        await self.ws.db.index_set_owner(
            state.run_id, agent=spec.agent, created_by=p.name, session=spec.session
        )
        if record_episodes and memory is not None and memory.write:
            await self.ws.db.index_patch(state.run_id, {"episode_scope": memory.write[0]})
        await self.ws.db.audit(p.name, "run.start", str(state.run_id), spec.agent or model)
        self.ws.supervisor.schedule(state.run_id)
        return await self._summary(state.run_id)

    async def runs(
        self,
        p: Principal,
        *,
        phase: RunPhase | None = None,
        agent: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Page[RunSummary]:
        status = None
        if phase is not None and phase in (RunPhase.COMPLETED, RunPhase.FAILED, RunPhase.CANCELLED):
            status = phase.value
        rows, total = await self.ws.db.index_list(
            owner=self._owner_filter(p),
            status=status,
            agent=agent,
            limit=500 if phase else limit,
            offset=0 if phase else offset,
        )
        items = [self._row_summary(row) for row in rows]
        if phase is not None:
            items = [s for s in items if s.phase is phase]
            total = len(items)
            items = items[offset : offset + limit]
        return Page[RunSummary](items=items, total=total)

    def _row_summary(self, row: dict[str, Any]) -> RunSummary:
        data = row["data"]
        active = self.ws.supervisor.active(row["run_id"])
        status = row["status"]
        phase = run_phase(
            status=status,
            awaiting=row["awaiting"] > 0,
            active=active,
            started=status != "pending" or bool(data.get("turns")),
            stopped=bool(data.get("stopped")),
        )
        error = data.get("error") or data.get("stopped")
        return RunSummary(
            run_id=row["run_id"],
            phase=phase,
            status=status,
            agent=row["agent"],
            created_by=row["created_by"],
            session=row["session"],
            model=data.get("model", ""),
            provider=data.get("provider", ""),
            protocol=data.get("protocol", ""),
            input_preview=data.get("input_preview", ""),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            awaiting_approvals=row["awaiting"],
            turns=data.get("turns", 0),
            tool_calls=data.get("tool_calls", 0),
            usage=UsageView.model_validate(data.get("usage") or {}),
            error=error,
            duration_s=max((row["updated_at"] - row["created_at"]).total_seconds(), 0.0),
        )

    async def _summary(self, run_id: UUID) -> RunSummary:
        row = await self.ws.db.index_get(run_id)
        if row is None:
            raise NotFound(f"no run {run_id}")
        return self._row_summary(row)

    async def _require_run(self, p: Principal, run_id: UUID) -> dict[str, Any]:
        row = await self.ws.db.index_get(run_id)
        if row is None or not self._can_view(p, row):
            raise NotFound(f"no run {run_id}")  # not revealing runs the caller cannot see
        return row

    def _can_view(self, p: Principal, row: dict[str, Any]) -> bool:
        return (
            p.is_admin
            or self.ws.config.server.run_visibility == "workspace"
            or row["created_by"] == p.name
        )

    def _owner_filter(self, p: Principal) -> str | None:
        return None if p.is_admin or self.ws.config.server.run_visibility == "workspace" else p.name

    async def run(self, p: Principal, run_id: UUID) -> RunDetail:
        row = await self._require_run(p, run_id)
        events = await self.ws.runs.read(run_id)
        return detail(
            fold(events),
            events,
            active=self.ws.supervisor.active(run_id),
            agent=row["agent"],
            created_by=row["created_by"],
            session=row["session"],
            error=row["data"].get("stopped"),
        )

    async def run_summary(self, p: Principal, run_id: UUID) -> RunSummary:
        await self._require_run(p, run_id)
        return await self._summary(run_id)

    async def run_events(self, p: Principal, run_id: UUID, after: int = 0) -> list[EventView]:
        await self._require_run(p, run_id)
        events = await self.ws.runs.read(run_id)
        return [v for v in event_views(events) if v.sequence > after]

    async def resume_run(self, p: Principal, run_id: UUID) -> RunSummary:
        row = await self._require_run(p, run_id)
        _owner_or_admin(p, row)
        if row["status"] in TERMINAL_STATUSES:
            raise Conflict(f"run {run_id} is {row['status']}")
        await self.ws.db.index_patch(run_id, {"stopped": None})
        self.ws.supervisor.schedule(run_id)
        return await self._summary(run_id)

    async def cancel_run(self, p: Principal, run_id: UUID, spec: CancelInput) -> RunSummary:
        row = await self._require_run(p, run_id)
        _owner_or_admin(p, row)
        await self.ws.supervisor.cancel(run_id, by=p.name, reason=spec.reason)
        await self.ws.db.audit(p.name, "run.cancel", str(run_id), spec.reason)
        return await self._summary(run_id)

    async def wait_run(self, p: Principal, run_id: UUID, *, timeout_s: float = 300.0) -> RunSummary:
        """Wait until nothing drives the run any more (it finished, waits for approval or
        stopped) or `timeout_s` passes, and return its summary."""
        await self._require_run(p, run_id)
        deadline = time.monotonic() + timeout_s
        while True:
            state = await load_run(self.ws.runs, run_id)
            remaining = deadline - time.monotonic()
            if state.status.is_terminal or not self.ws.supervisor.active(run_id) or remaining <= 0:
                return await self._summary(run_id)
            await self.ws.bus.wait_until(self._progress(run_id, state.version), remaining)

    def _progress(self, run_id: UUID, after: int) -> Callable[[], bool]:
        """True once the run has a newer event or nothing drives it any more."""
        return lambda: self.ws.bus.version(run_id) > after or not self.ws.supervisor.active(run_id)

    async def watch(
        self, p: Principal, run_id: UUID, after: int = 0, *, heartbeat: float = 15.0
    ) -> AsyncGenerator[EventView | None]:
        """Events after `after`, then live events until the run is terminal. Yields None when
        nothing happened for `heartbeat` seconds (for keep-alives)."""
        await self._require_run(p, run_id)
        last = after
        while True:
            events = await self.ws.runs.read(run_id)
            for view in event_views(events):
                if view.sequence > last:
                    last = view.sequence
                    yield view
            if fold(events).status.is_terminal:
                return
            if not await self.ws.bus.wait(run_id, last, timeout_s=heartbeat):
                yield None

    # --- approvals ------------------------------------------------------------------------------

    async def approvals(self, p: Principal) -> list[Approval]:
        rows = await self.ws.db.query(
            "SELECT run_id FROM run_index WHERE awaiting > 0 ORDER BY updated_at DESC LIMIT 200"
        )
        result: list[Approval] = []
        for (raw_id,) in rows:
            run_id = UUID(raw_id)
            row = await self.ws.db.index_get(run_id)
            if row is None or not self._can_view(p, row):
                continue
            state = await load_run(self.ws.runs, run_id)
            decided = {i.invocation_id: i.decided_at for i in state.awaiting_approval}
            for request in await self.ws.runner.pending_approvals(run_id):
                result.append(
                    Approval(
                        run_id=run_id,
                        agent=row["agent"],
                        invocation_id=request.invocation_id,
                        tool=request.tool.name,
                        effect=request.tool.effect.value,
                        trust=request.tool.trust.value,
                        idempotent=request.tool.idempotent,
                        arguments=request.arguments,
                        reason=request.reason,
                        binding=request.binding,
                        expires_at=request.expires_at,
                        requested_at=decided.get(request.invocation_id),
                        run_input=state.input[:500],
                    )
                )
        return result

    async def decide(
        self, p: Principal, run_id: UUID, invocation_id: str, decision: ApprovalDecisionInput
    ) -> RunSummary:
        row = await self._require_run(p, run_id)
        if not (p.is_admin or p.can_approve):
            raise Forbidden("this principal cannot approve tool calls")
        pending = {r.invocation_id: r for r in await self.ws.runner.pending_approvals(run_id)}
        request = pending.get(invocation_id)
        if request is None:
            raise NotFound(f"no pending approval {invocation_id!r} in run {run_id}")
        await self.ws.runner.resolve_approval(
            run_id,
            ApprovalDecision(
                binding=request.binding,
                approved=decision.approved,
                approver=p.name,
                reason=decision.reason,
            ),
        )
        verb = "approve" if decision.approved else "deny"
        await self.ws.db.audit(p.name, f"approval.{verb}", invocation_id, decision.reason or "")
        del row
        self.ws.supervisor.schedule(run_id)
        return await self._summary(run_id)

    # --- artifacts ------------------------------------------------------------------------------

    async def artifact(
        self, p: Principal, run_id: UUID, artifact_id: str, *, offset: int = 0, length: int = 20_000
    ) -> ArtifactView:
        await self._require_run(p, run_id)
        artifact = await self.ws.artifacts.get(run_id, artifact_id)
        if artifact is None:
            raise NotFound(f"no artifact {artifact_id} in run {run_id}")
        end = min(offset + max(length, 1), artifact.ref.chars)
        return ArtifactView(
            artifact_id=artifact_id,
            run_id=run_id,
            chars=artifact.ref.chars,
            media_type=artifact.ref.media_type,
            source=artifact.source,
            trust=artifact.trust.value,
            offset=offset,
            text=artifact.text[offset:end],
            truncated=end < artifact.ref.chars,
        )

    # --- knowledge ------------------------------------------------------------------------------

    async def _knowledge_access(self, p: Principal) -> KnowledgeAccess:
        if p.is_admin:
            collections = [c.name for c in await self.ws.knowledge_store.collections()]
            labels: set[str] = set()
            for name in collections:
                for source in await self.ws.knowledge_store.sources(name):
                    labels.update(source.labels)
            return KnowledgeAccess(collections=tuple(collections), labels=tuple(sorted(labels)))
        return p.knowledge or KnowledgeAccess()

    async def collections(self, p: Principal) -> list[CollectionView]:
        access = await self._knowledge_access(p)
        views: list[CollectionView] = []
        for collection in await self.ws.knowledge_store.collections():
            if collection.name not in access.collections:
                continue
            sources = [
                s
                for s in await self.ws.knowledge_store.sources(collection.name)
                if access.can_see(s.collection, s.labels)
            ]
            views.append(
                CollectionView(
                    name=collection.name,
                    description=collection.description,
                    embedding=collection.space.model if collection.space else None,
                    sources=len(sources),
                    chunks=sum(s.chunk_count for s in sources),
                    created_at=collection.created_at,
                )
            )
        return views

    async def create_collection(self, p: Principal, spec: CollectionInput) -> CollectionView:
        _admin(p)
        if await self.ws.knowledge_store.collection(spec.name) is not None:
            raise Conflict(f"collection {spec.name!r} already exists")
        await self.ws.knowledge.create_collection(
            spec.name, spec.description, embedded=spec.embedded, actor=p.name
        )
        return next(c for c in await self.collections(p) if c.name == spec.name)

    async def delete_collection(self, p: Principal, name: str) -> int:
        _admin(p)
        removed = await self.ws.knowledge.delete_collection(
            name, actor=p.name, reason="deleted through the API"
        )
        if removed < 0:
            raise NotFound(f"no collection {name!r}")
        await self.ws.db.audit(p.name, "collection.delete", name, f"{removed} sources")
        return removed

    async def sources(self, p: Principal, collection: str) -> list[SourceView]:
        access = await self._knowledge_access(p)
        if collection not in access.collections:
            raise NotFound(f"no collection {collection!r}")
        sources = await self.ws.knowledge_store.sources(collection)
        return [_source_view(s) for s in sources if access.can_see(s.collection, s.labels)]

    async def source(self, p: Principal, collection: str, source_id: str) -> SourceDetail:
        access = await self._knowledge_access(p)
        source = await self.ws.knowledge_store.visible_source(access, source_id)
        if source is None or source.collection != collection:
            raise NotFound(f"no source {source_id!r} in {collection!r}")
        sections = await self.ws.knowledge_store.source_sections(access, source_id)
        chunks = await self.ws.knowledge_store.source_chunks(access, source_id)
        counts: dict[str, int] = {}
        for chunk in chunks:
            counts[chunk.section_id] = counts.get(chunk.section_id, 0) + 1
        return SourceDetail(
            source=_source_view(source),
            sections=tuple(
                SectionView(
                    index=s.index,
                    title=s.title,
                    level=s.level,
                    path=s.path,
                    chunks=counts.get(s.section_id, 0),
                )
                for s in sections
            ),
            chunks=tuple(_chunk_view(c) for c in chunks[:500]),
        )

    async def ingest(
        self,
        p: Principal,
        collection: str,
        *,
        key: str,
        data: bytes,
        filename: str | None = None,
        labels: tuple[str, ...] = (),
        trust: SourceTrust = SourceTrust.INTERNAL,
    ) -> IngestResultView:
        _admin(p)
        if await self.ws.knowledge_store.collection(collection) is None:
            raise NotFound(f"no collection {collection!r}")
        try:
            result = await self.ws.knowledge.ingest_bytes(
                collection,
                key,
                data,
                name=filename or key,
                labels=labels,
                trust=trust,
                actor=p.name,
            )
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        await self.ws.db.audit(p.name, f"knowledge.{result.action}", f"{collection}/{key}")
        return IngestResultView(
            action=result.action,
            source=_source_view(result.source) if result.source else None,
            reason=result.reason,
        )

    async def sync(self, p: Principal, collection: str, spec: SyncInput) -> SyncResultView:
        _admin(p)
        target = (self.ws.root / spec.path).resolve()
        roots = [(self.ws.root / r).resolve() for r in self.ws.config.knowledge.sync_roots]
        if not any(target == r or target.is_relative_to(r) for r in roots):
            raise Forbidden(f"{spec.path} is outside the configured knowledge.sync_roots")
        if not target.is_dir():
            raise InvalidRequest(f"{spec.path} is not a directory")
        if await self.ws.knowledge_store.collection(collection) is None:
            raise NotFound(f"no collection {collection!r}")
        report = await self.ws.knowledge.sync_directory(
            collection, target, labels=spec.labels, trust=spec.trust, actor=p.name
        )
        await self.ws.db.audit(p.name, "knowledge.sync", collection, str(target))
        return SyncResultView(**report.model_dump())

    async def delete_source(self, p: Principal, collection: str, key: str) -> None:
        _admin(p)
        if not await self.ws.knowledge.delete_source(
            collection, key, actor=p.name, reason="deleted through the API"
        ):
            raise NotFound(f"no source {key!r} in {collection!r}")
        await self.ws.db.audit(p.name, "knowledge.delete", f"{collection}/{key}")

    async def relabel(
        self, p: Principal, collection: str, key: str, labels: tuple[str, ...]
    ) -> SourceView:
        _admin(p)
        try:
            await self.ws.knowledge.set_labels(collection, key, labels, actor=p.name)
        except LookupError as exc:
            raise NotFound(str(exc)) from exc
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        source = await self.ws.knowledge_store.source(collection, key)
        assert source is not None  # noqa: S101  # just relabelled
        return _source_view(source)

    async def search(self, p: Principal, spec: SearchInput) -> SearchResultView:
        access = await self._knowledge_access(p)
        started = time.perf_counter()
        passages = await self.ws.retriever.retrieve(
            KnowledgeQuery(
                text=spec.query, access=access, collections=spec.collections, limit=spec.limit
            )
        )
        keys: dict[str, str] = {}
        hits: list[SearchHit] = []
        for passage in passages:
            if passage.source_id not in keys:
                source = await self.ws.knowledge_store.visible_source(access, passage.source_id)
                keys[passage.source_id] = source.key if source else passage.source_id
            hits.append(SearchHit(passage=passage, source_key=keys[passage.source_id]))
        notes = () if access.collections else ("no knowledge collections are granted to you",)
        return SearchResultView(
            query=spec.query,
            hits=tuple(hits),
            notes=notes,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    async def reconcile_knowledge(self, p: Principal) -> dict[str, int]:
        _admin(p)
        report = await self.ws.knowledge.reconcile(repair=True)
        return report.model_dump()

    # --- memory ---------------------------------------------------------------------------------

    async def _memory_scopes(self, p: Principal, *, write: bool = False) -> set[str] | None:
        """None means every scope (admins)."""
        if p.is_admin:
            return None
        if p.memory is None:
            return set()
        return set(p.memory.write if write else p.memory.read)

    async def memories(
        self,
        p: Principal,
        *,
        scope: str | None = None,
        kind: MemoryKind | None = None,
        status: str | None = None,
        query: str | None = None,
        limit: int = 200,
    ) -> list[MemoryView]:
        allowed = await self._memory_scopes(p)
        records = await self.ws.memory_store.records(scope)
        needle = query.casefold() if query else None
        views = [
            _memory_view(r)
            for r in records
            if (allowed is None or r.scope in allowed)
            and (kind is None or r.kind is kind)
            and (status is None or r.status.value == status)
            and (needle is None or needle in r.content.casefold() or needle in (r.subject or ""))
        ]
        views.sort(key=lambda v: v.recorded_at, reverse=True)
        return views[:limit]

    async def memory_scopes(self, p: Principal) -> list[str]:
        allowed = await self._memory_scopes(p)
        present = {r.scope for r in await self.ws.memory_store.records()}
        if allowed is not None:
            present |= allowed
            present &= allowed
        return sorted(present)

    async def memory(self, p: Principal, memory_id: str) -> MemoryDetail:
        record = await self.ws.memory_store.get(memory_id)
        allowed = await self._memory_scopes(p)
        if record is None or (allowed is not None and record.scope not in allowed):
            raise NotFound(f"no memory {memory_id!r}")
        history = await self.ws.memory.history(memory_id)
        return MemoryDetail(
            memory=_memory_view(record),
            history=tuple(
                MemoryChangeView(at=c.at, action=c.action.value, actor=c.actor, detail=c.detail)
                for c in history
            ),
        )

    async def remember(self, p: Principal, spec: RememberInput) -> WriteResultView:
        _operator(p)
        allowed = await self._memory_scopes(p, write=True)
        if allowed is not None and spec.scope not in allowed:
            raise Forbidden(f"scope {spec.scope!r} is not writable for {p.name}")
        try:
            candidate = MemoryCandidate(
                scope=spec.scope,
                kind=spec.kind,
                content=spec.content,
                subject=spec.subject,
                key=spec.key,
                change=ChangeKind(spec.change),
                certainty=Certainty.CONFIRMED,
                provenance=Provenance(kind=SourceKind.USER, actor=p.name),
                valid_until=spec.valid_until,
            )
        except ValueError as exc:
            raise InvalidRequest(str(exc)) from exc
        outcome = await self.ws.memory.remember(
            candidate, MemoryAccess(read=(spec.scope,), write=(spec.scope,))
        )
        return WriteResultView(
            action=outcome.action.value,
            memory=_memory_view(outcome.record) if outcome.record else None,
            affected=outcome.affected,
            reason=outcome.reason,
        )

    async def _writable_record(self, p: Principal, memory_id: str) -> MemoryRecord:
        record = await self.ws.memory_store.get(memory_id)
        readable = await self._memory_scopes(p)
        if record is None or (readable is not None and record.scope not in readable):
            raise NotFound(f"no memory {memory_id!r}")
        writable = await self._memory_scopes(p, write=True)
        if writable is not None and record.scope not in writable:
            raise Forbidden(f"memory {memory_id} is in a read-only scope for {p.name}")
        return record

    async def retract_memory(self, p: Principal, memory_id: str, spec: ForgetInput) -> MemoryView:
        _operator(p)
        record = await self._writable_record(p, memory_id)
        access = MemoryAccess(read=(record.scope,), write=(record.scope,))
        retracted = await self.ws.memory.retract(
            memory_id, access, reason=spec.reason, actor=p.name
        )
        return _memory_view(retracted)

    async def forget_memory(self, p: Principal, memory_id: str, spec: ForgetInput) -> None:
        _operator(p)
        record = await self._writable_record(p, memory_id)
        access = MemoryAccess(read=(record.scope,), write=(record.scope,))
        await self.ws.memory.forget(memory_id, access, reason=spec.reason, actor=p.name)
        await self.ws.db.audit(p.name, "memory.forget", memory_id, spec.reason)

    async def maintain_memory(self, p: Principal) -> MaintenanceView:
        _admin(p)
        report = await self.ws.memory.maintain()
        return MaintenanceView(expired=report.expired, deleted=report.deleted)


# --- helpers -----------------------------------------------------------------------------------


def _admin(p: Principal) -> None:
    if not p.is_admin:
        raise Forbidden("this action needs the admin role")


def _operator(p: Principal) -> None:
    if p.role is Role.VIEWER:
        raise Forbidden("viewers cannot change anything")


def _owner_or_admin(p: Principal, row: dict[str, Any]) -> None:
    if not (p.is_admin or row["created_by"] == p.name):
        raise Forbidden("only the run's creator or an admin can do that")


def _effective_memory(configured: MemoryAccess | None, p: Principal) -> MemoryAccess | None:
    if configured is None or p.is_admin:
        return configured
    grant = p.memory or MemoryAccess()
    read = tuple(s for s in configured.read if s in grant.read)
    write = tuple(s for s in configured.write if s in grant.write and s in read)
    return MemoryAccess(read=read, write=write) if read else None


def _effective_knowledge(
    configured: KnowledgeAccess | None, p: Principal
) -> KnowledgeAccess | None:
    if configured is None or p.is_admin:
        return configured
    grant = p.knowledge or KnowledgeAccess()
    collections = tuple(c for c in configured.collections if c in grant.collections)
    labels = tuple(label for label in configured.labels if label in grant.labels)
    return KnowledgeAccess(collections=collections, labels=labels) if collections else None


def _merge(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)  # pyright: ignore[reportUnknownArgumentType]  # JSON objects
        else:
            merged[key] = value
    return merged


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error["loc"])
    return f"{location}: {error['msg']}" if location else str(error["msg"])


def _source_view(source: Source) -> SourceView:
    return SourceView(
        source_id=source.source_id,
        collection=source.collection,
        key=source.key,
        title=source.title,
        media_type=source.media_type,
        version=source.version,
        labels=source.labels,
        trust=source.trust,
        effective_date=source.effective_date,
        ingested_at=source.ingested_at,
        size_bytes=source.size_bytes,
        chunks=source.chunk_count,
        flagged_chunks=int(source.extraction.get("chunks_flagged", "0")),
        extraction=source.extraction,
    )


def _chunk_view(chunk: Chunk) -> ChunkView:
    return ChunkView(
        chunk_id=chunk.chunk_id,
        ordinal=chunk.ordinal,
        section=" > ".join(chunk.path),
        location=chunk.location_label(),
        text=chunk.text,
        flags=chunk.flags,
        tokens=chunk.tokens,
    )


def _memory_view(record: MemoryRecord) -> MemoryView:
    return MemoryView(
        memory_id=record.memory_id,
        scope=record.scope,
        kind=record.kind,
        content=record.content,
        subject=record.subject,
        key=record.key,
        certainty=record.certainty,
        status=record.status.value,
        status_reason=record.status_reason,
        source=record.provenance.label(),
        external=record.provenance.external,
        recorded_at=record.recorded_at,
        valid_from=record.valid_from,
        valid_until=record.valid_until,
        supersedes=record.supersedes,
        superseded_by=record.superseded_by,
        conflicts_with=record.conflicts_with,
        tags=record.tags,
        reinforced=record.reinforced,
        recall_count=record.recall_count,
        last_recalled_at=record.last_recalled_at,
    )
