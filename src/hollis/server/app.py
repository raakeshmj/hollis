"""The Hollis REST API: HTTP over the Platform services (ADR-0027).

- Versioned under `/v1`, described by OpenAPI at `/v1/openapi.json`.
- Errors are RFC 9457 problem details (`application/problem+json`) with a stable `code`.
- Long-running work: starting a run returns 202 at once; follow it with Server-Sent Events
  (`GET /v1/runs/{id}/stream`, resumable with `Last-Event-ID`) or long polling
  (`GET /v1/runs/{id}/summary?wait_s=30`). Runs execute in the server process and do not depend
  on any connection staying open.
- Authentication is always required (ADR-0028): `Authorization: Bearer <token>`, or the browser
  session cookie set by `POST /v1/session`. Cookie-authenticated requests that change anything
  must come from the server's own origin; no CORS headers are ever sent.
- Every response carries a restrictive Content-Security-Policy and related headers; requests
  for hosts other than the configured ones are refused (DNS-rebinding defence).

Authorization is not decided here: every route passes the authenticated principal to the
platform, which enforces it. The web application is served from the same origin.
"""

import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hollis import __version__
from hollis.platform.auth import Principal
from hollis.platform.errors import (
    Forbidden,
    InvalidRequest,
    NotFound,
    PlatformError,
    Unauthenticated,
    Unavailable,
)
from hollis.platform.schemas import (
    API_VERSION,
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
    LabelsInput,
    LoginInput,
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
    Session,
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

log = logging.getLogger("hollis.server")

SESSION_COOKIE = "hollis_session"
STATIC_DIR = Path(__file__).parent / "static"
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

SECURITY_HEADERS = (
    (
        "content-security-policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'",
    ),
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "no-referrer"),
    ("cross-origin-opener-policy", "same-origin"),
    ("cross-origin-resource-policy", "same-origin"),
    ("permissions-policy", "camera=(), microphone=(), geolocation=(), payment=()"),
)

NOT_BUILT = """<!doctype html><html lang="en"><meta charset="utf-8"><title>Hollis</title>
<body>
<h1>Hollis</h1><p>The API is running at <code>/v1</code> (see <code>/v1/openapi.json</code>).
The web application has not been built into this installation; see docs/guides/web.md.</p>
</body></html>"""


# --- problem details --------------------------------------------------------------------------


def problem(status: int, code: str, title: str, detail: str, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"urn:hollis:problem:{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
        **extra,
    }
    headers = {"www-authenticate": 'Bearer realm="hollis"'} if status == 401 else None
    return JSONResponse(body, status, headers=headers, media_type="application/problem+json")


_HTTP_CODES = {
    400: ("bad_request", "Bad request"),
    401: ("unauthenticated", "Authentication required"),
    403: ("forbidden", "Not allowed"),
    404: ("not_found", "Not found"),
    405: ("method_not_allowed", "Method not allowed"),
    413: ("too_large", "Request too large"),
}


async def _platform_error(request: Request, exc: Exception) -> Response:
    error = cast(PlatformError, exc)
    return problem(error.status, error.code, error.title, error.detail)


async def _validation_error(request: Request, exc: Exception) -> Response:
    errors = [
        {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
        for e in cast(RequestValidationError, exc).errors()
    ]  # inputs are not echoed: they may contain things that should not be logged or shown
    return problem(
        422, "invalid_request", "Invalid request", "the request is invalid", errors=errors
    )


async def _http_error(request: Request, exc: Exception) -> Response:
    error = cast(StarletteHTTPException, exc)
    code, title = _HTTP_CODES.get(error.status_code, ("error", "Error"))
    return problem(error.status_code, code, title, str(error.detail))


async def _unexpected(request: Request, exc: Exception) -> Response:
    log.error("unhandled error on %s %s: %s", request.method, request.url.path, type(exc).__name__)
    return problem(500, "error", "Unexpected error", "the server failed to handle this request")


# --- authentication ---------------------------------------------------------------------------


def _get_platform(request: Request) -> Platform:
    platform = getattr(request.app.state, "platform", None)
    if not isinstance(platform, Platform):
        raise Unavailable("the server is starting or stopping")
    return platform


Plat = Annotated[Platform, Depends(_get_platform)]


async def _authenticate(request: Request, platform: Plat) -> Principal:
    header = request.headers.get("authorization")
    if header is not None:
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise Unauthenticated("use an Authorization: Bearer <token> header")
        return await platform.authenticate(token.strip())
    session = request.cookies.get(SESSION_COOKIE)
    if session:
        principal, _ = await platform.ws.auth.session(session)
        if request.method not in SAFE_METHODS:
            _require_same_origin(request)
        return principal
    raise Unauthenticated("authentication required: a bearer token or a session")


def _require_same_origin(request: Request) -> None:
    """Cookies are ambient credentials: a browser attaches them to requests other sites trigger.
    SameSite=Strict already withholds them cross-site; this check refuses any state-changing
    cookie request whose Origin is not this server."""
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    if origin is None or urlsplit(origin).netloc.lower() != host.lower():
        raise Forbidden("cross-origin request refused")


Caller = Annotated[Principal, Depends(_authenticate)]


# --- routes -----------------------------------------------------------------------------------

api = APIRouter(prefix="/v1")


@api.get("/ping", tags=["system"])
async def ping() -> dict[str, str]:
    """Liveness. The only route that needs no authentication; it reveals nothing else."""
    return {"status": "ok"}


@api.post("/session", tags=["identity"])
async def login(spec: LoginInput, request: Request, response: Response, platform: Plat) -> Session:
    """Exchange an API token or a single-use login token for a browser session cookie."""
    if request.headers.get("origin") is not None:
        _require_same_origin(request)  # no other site may sign a browser in (login CSRF)
    hours = platform.ws.config.server.session_hours
    session, expires, principal = await platform.ws.auth.open_session(spec.token, hours=hours)
    response.set_cookie(
        SESSION_COOKIE,
        session,
        max_age=int(hours * 3600),
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/",
    )
    await platform.ws.db.audit(principal.name, "session.open", "browser")
    return Session(principal=principal.view(), expires_at=expires)


@api.post("/session/link", tags=["identity"])
async def login_link(platform: Plat, caller: Caller) -> dict[str, str]:
    """A single-use, ten-minute sign-in token for yourself: open `/login#token=<token>`."""
    return {"token": await platform.login_link_token(caller)}


@api.get("/session", tags=["identity"])
async def current_session(request: Request, caller: Caller, platform: Plat) -> Session:
    expires = None
    session = request.cookies.get(SESSION_COOKIE)
    if session and "authorization" not in request.headers:
        _, expires = await platform.ws.auth.session(session)
    return Session(principal=caller.view(), expires_at=expires)


@api.delete("/session", tags=["identity"], status_code=204)
async def logout(request: Request, platform: Plat) -> Response:
    session = request.cookies.get(SESSION_COOKIE)
    if session:
        if request.headers.get("origin") is not None:
            _require_same_origin(request)
        await platform.ws.auth.close_session(session)
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response


@api.get("/health", tags=["system"])
async def health(platform: Plat, caller: Caller) -> Health:
    return await platform.health(caller)


@api.get("/settings", tags=["system"])
async def settings(platform: Plat, caller: Caller) -> SettingsView:
    return await platform.settings(caller)


@api.put("/settings/config", tags=["system"])
async def update_config(config: dict[str, Any], platform: Plat, caller: Caller) -> SettingsView:
    """Replace the workspace configuration (admin). Validated before anything is saved."""
    return await platform.update_config(caller, config)


@api.patch("/settings/config", tags=["system"])
async def patch_config(changes: dict[str, Any], platform: Plat, caller: Caller) -> SettingsView:
    """Change some settings (admin): merged into what hollis.toml sets, validated, applied."""
    return await platform.patch_config(caller, changes)


@api.get("/audit", tags=["system"])
async def audit(
    platform: Plat, caller: Caller, limit: Annotated[int, Query(ge=1, le=1000)] = 100
) -> list[dict[str, str]]:
    return await platform.audit(caller, limit)


# identity


@api.get("/me", tags=["identity"])
async def whoami(platform: Plat, caller: Caller) -> PrincipalView:
    return await platform.whoami(caller)


@api.get("/principals", tags=["identity"])
async def principals(platform: Plat, caller: Caller) -> list[PrincipalView]:
    return await platform.principals(caller)


@api.post("/principals", tags=["identity"], status_code=201)
async def create_principal(spec: PrincipalInput, platform: Plat, caller: Caller) -> PrincipalView:
    return await platform.create_principal(caller, spec)


@api.get("/tokens", tags=["identity"])
async def tokens(platform: Plat, caller: Caller) -> list[TokenView]:
    return await platform.tokens(caller)


@api.post("/tokens", tags=["identity"], status_code=201)
async def create_token(spec: TokenInput, platform: Plat, caller: Caller) -> CreatedToken:
    """The token value is in this response only; it is stored as a hash."""
    return await platform.create_token(caller, spec)


@api.delete("/tokens/{token_id}", tags=["identity"], status_code=204)
async def revoke_token(token_id: str, platform: Plat, caller: Caller) -> Response:
    await platform.revoke_token(caller, token_id)
    return Response(status_code=204)


# models


@api.get("/deployments", tags=["models"])
async def deployments(platform: Plat, caller: Caller) -> list[DeploymentView]:
    return await platform.deployments(caller)


@api.get("/deployments/{deployment_id}", tags=["models"])
async def deployment(deployment_id: str, platform: Plat, caller: Caller) -> DeploymentView:
    return await platform.deployment(caller, deployment_id)


@api.put("/deployments/{deployment_id}", tags=["models"])
async def put_deployment(
    deployment_id: str, spec: DeploymentInput, platform: Plat, caller: Caller
) -> DeploymentView:
    """Create or replace a deployment (admin). Credentials are referenced by environment
    variable name; values are never accepted or returned."""
    if spec.deployment.get("id") != deployment_id:
        raise InvalidRequest("the deployment id in the body must match the URL")
    return await platform.upsert_deployment(caller, spec)


@api.delete("/deployments/{deployment_id}", tags=["models"], status_code=204)
async def delete_deployment(deployment_id: str, platform: Plat, caller: Caller) -> Response:
    await platform.delete_deployment(caller, deployment_id)
    return Response(status_code=204)


@api.post("/deployments/{deployment_id}/probe", tags=["models"])
async def probe(deployment_id: str, platform: Plat, caller: Caller) -> ProbeResult:
    """Send one tiny request to the deployment (an explicit network call, may cost money)."""
    return await platform.probe_deployment(caller, deployment_id)


# tools


@api.get("/tools", tags=["tools"])
async def tools(platform: Plat, caller: Caller) -> list[ToolView]:
    return await platform.tools(caller)


@api.get("/tools/activity", tags=["tools"])
async def tool_activity(
    platform: Plat, caller: Caller, limit: Annotated[int, Query(ge=1, le=500)] = 50
) -> list[ToolActivity]:
    return await platform.tool_activity(caller, limit)


@api.get("/mcp/servers", tags=["tools"])
async def mcp_servers(platform: Plat, caller: Caller) -> list[McpServerView]:
    return await platform.mcp_servers(caller)


@api.post("/mcp/servers/{name}/connect", tags=["tools"])
async def connect_mcp(name: str, platform: Plat, caller: Caller) -> McpServerView:
    return await platform.connect_mcp(caller, name)


# agents


@api.get("/agents", tags=["agents"])
async def agents(platform: Plat, caller: Caller) -> list[Agent]:
    return await platform.agents(caller)


@api.post("/agents", tags=["agents"], status_code=201)
async def create_agent(spec: AgentInput, platform: Plat, caller: Caller) -> Agent:
    return await platform.create_agent(caller, spec)


@api.post("/agents/starter", tags=["agents"])
async def create_starter(platform: Plat, caller: Caller) -> Agent:
    """Create the starter `docs` collection and default agent (admin; idempotent)."""
    return await platform.create_starter(caller)


@api.get("/agents/{name}", tags=["agents"])
async def agent(name: str, platform: Plat, caller: Caller) -> Agent:
    return await platform.agent(caller, name)


@api.put("/agents/{name}", tags=["agents"])
async def update_agent(name: str, spec: AgentInput, platform: Plat, caller: Caller) -> Agent:
    return await platform.update_agent(caller, name, spec)


@api.delete("/agents/{name}", tags=["agents"], status_code=204)
async def delete_agent(name: str, platform: Plat, caller: Caller) -> Response:
    await platform.delete_agent(caller, name)
    return Response(status_code=204)


@api.post("/agents/from-template", tags=["agents"], status_code=201)
async def create_agent_from_template(
    spec: FromTemplateInput, platform: Plat, caller: Caller
) -> Agent:
    """Create an ordinary agent from a template (admin). Hard requirement failures answer 422
    with the reasons."""
    return await platform.create_agent_from_template(caller, spec)


@api.get("/agents/{name}/upgrade", tags=["agents"])
async def upgrade_preview(name: str, platform: Plat, caller: Caller) -> UpgradePreview:
    """What upgrading the agent to its template's current version would change (admin)."""
    return await platform.upgrade_preview(caller, name)


@api.post("/agents/{name}/upgrade", tags=["agents"])
async def upgrade_agent(name: str, spec: UpgradeInput, platform: Plat, caller: Caller) -> Agent:
    """Apply the upgrade the preview showed; 409 if the template changed since (admin)."""
    return await platform.upgrade_agent(caller, name, spec)


@api.post("/agents/{name}/export", tags=["agents"])
async def export_agent(
    name: str, spec: ExportInput, platform: Plat, caller: Caller
) -> TemplateExport:
    """The agent as a template file to share by copying (admin)."""
    return await platform.export_agent(caller, name, spec)


# specialization


@api.get("/templates", tags=["specialization"])
async def templates(platform: Plat, caller: Caller) -> list[TemplateView]:
    return await platform.templates(caller)


@api.get("/templates/{template_id}", tags=["specialization"])
async def template(template_id: str, platform: Plat, caller: Caller) -> TemplateDetail:
    return await platform.template(caller, template_id)


@api.get("/capabilities", tags=["specialization"])
async def capabilities(platform: Plat, caller: Caller) -> list[CapabilityView]:
    return await platform.capabilities(caller)


@api.get("/contracts", tags=["specialization"])
async def contracts(platform: Plat, caller: Caller) -> list[ContractView]:
    return await platform.contracts(caller)


@api.get("/skills", tags=["specialization"])
async def skills(platform: Plat, caller: Caller) -> SkillsView:
    return await platform.skills(caller)


# runs


@api.get("/runs", tags=["runs"])
async def runs(
    platform: Plat,
    caller: Caller,
    phase: RunPhase | None = None,
    agent: str | None = None,
    roots: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[RunSummary]:
    """Runs the caller can see, newest first. `roots=true` leaves out delegated runs."""
    return await platform.runs(
        caller, phase=phase, agent=agent, roots=roots, limit=limit, offset=offset
    )


@api.post("/runs", tags=["runs"], status_code=202)
async def start_run(
    spec: StartRun, response: Response, platform: Plat, caller: Caller
) -> RunSummary:
    """Start a run in the background. Follow it with /stream (SSE) or /summary?wait_s=."""
    started = await platform.start_run(caller, spec)
    response.headers["location"] = f"/v1/runs/{started.run_id}"
    return started


@api.get("/runs/{run_id}", tags=["runs"])
async def run(run_id: UUID, platform: Plat, caller: Caller) -> RunDetail:
    return await platform.run(caller, run_id)


@api.get("/runs/{run_id}/tree", tags=["runs"])
async def run_tree(run_id: UUID, platform: Plat, caller: Caller) -> OrchestrationView:
    """The orchestration this run belongs to: its root, every run delegated below it that the
    caller can see, reports, refusals, disagreements and totals."""
    return await platform.run_tree(caller, run_id)


@api.get("/runs/{run_id}/summary", tags=["runs"])
async def run_summary(
    run_id: UUID,
    platform: Plat,
    caller: Caller,
    wait_s: Annotated[float, Query(ge=0, le=60)] = 0,
) -> RunSummary:
    """With wait_s > 0, waits (long polling) until nothing drives the run any more: it
    finished, waits for approval or stopped."""
    if wait_s > 0:
        return await platform.wait_run(caller, run_id, timeout_s=wait_s)
    return await platform.run_summary(caller, run_id)


@api.get("/runs/{run_id}/events", tags=["runs"])
async def run_events(
    run_id: UUID, platform: Plat, caller: Caller, after: Annotated[int, Query(ge=0)] = 0
) -> list[EventView]:
    return await platform.run_events(caller, run_id, after)


@api.get(
    "/runs/{run_id}/stream",
    tags=["runs"],
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}, "description": "Run events (SSE)"}},
)
async def stream_run(
    run_id: UUID,
    platform: Plat,
    caller: Caller,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Server-Sent Events: `run_event` events (id = event sequence) until the run finishes,
    then one `end` event with the run summary. Comments keep idle connections alive."""
    start = after
    if last_event_id is not None and last_event_id.isdigit():
        start = max(start, int(last_event_id))
    await platform.run_summary(caller, run_id)  # authorize before the stream starts

    async def events() -> AsyncIterator[str]:
        async for view in platform.watch(caller, run_id, start):
            if view is None:
                yield ": keep-alive\n\n"
            else:
                yield f"id: {view.sequence}\nevent: run_event\ndata: {view.model_dump_json()}\n\n"
        final = await platform.run_summary(caller, run_id)
        yield f"event: end\ndata: {final.model_dump_json()}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"cache-control": "no-store", "x-accel-buffering": "no"},
    )


@api.post("/runs/{run_id}/cancel", tags=["runs"])
async def cancel_run(
    run_id: UUID, platform: Plat, caller: Caller, spec: CancelInput | None = None
) -> RunSummary:
    return await platform.cancel_run(caller, run_id, spec or CancelInput())


@api.post("/runs/{run_id}/resume", tags=["runs"])
async def resume_run(run_id: UUID, platform: Plat, caller: Caller) -> RunSummary:
    return await platform.resume_run(caller, run_id)


@api.get("/runs/{run_id}/artifacts/{artifact_id}", tags=["runs"])
async def artifact(
    run_id: UUID,
    artifact_id: str,
    platform: Plat,
    caller: Caller,
    offset: Annotated[int, Query(ge=0)] = 0,
    length: Annotated[int, Query(ge=1, le=200_000)] = 20_000,
) -> ArtifactView:
    return await platform.artifact(caller, run_id, artifact_id, offset=offset, length=length)


# approvals


@api.get("/approvals", tags=["approvals"])
async def approvals(platform: Plat, caller: Caller) -> list[Approval]:
    return await platform.approvals(caller)


@api.post("/runs/{run_id}/approvals/{invocation_id:path}", tags=["approvals"])
async def decide(
    run_id: UUID,
    invocation_id: str,
    decision: ApprovalDecisionInput,
    platform: Plat,
    caller: Caller,
) -> RunSummary:
    """Approve or deny one pending tool call, as the authenticated principal. Invocation ids
    contain slashes (`<run>/turn/<n>/call/<i>`) and are used verbatim in the path."""
    return await platform.decide(caller, run_id, invocation_id, decision)


# knowledge


@api.get("/knowledge/collections", tags=["knowledge"])
async def collections(platform: Plat, caller: Caller) -> list[CollectionView]:
    return await platform.collections(caller)


@api.post("/knowledge/collections", tags=["knowledge"], status_code=201)
async def create_collection(
    spec: CollectionInput, platform: Plat, caller: Caller
) -> CollectionView:
    return await platform.create_collection(caller, spec)


@api.delete("/knowledge/collections/{name}", tags=["knowledge"], status_code=204)
async def delete_collection(name: str, platform: Plat, caller: Caller) -> Response:
    await platform.delete_collection(caller, name)
    return Response(status_code=204)


@api.get("/knowledge/collections/{name}/sources", tags=["knowledge"])
async def sources(name: str, platform: Plat, caller: Caller) -> list[SourceView]:
    return await platform.sources(caller, name)


@api.get("/knowledge/collections/{name}/sources/{source_id}", tags=["knowledge"])
async def source(name: str, source_id: str, platform: Plat, caller: Caller) -> SourceDetail:
    return await platform.source(caller, name, source_id)


@api.put("/knowledge/collections/{name}/documents/{key:path}", tags=["knowledge"])
async def put_document(
    name: str,
    key: str,
    request: Request,
    platform: Plat,
    caller: Caller,
    labels: Annotated[list[str] | None, Query()] = None,
    trust: SourceTrust = SourceTrust.INTERNAL,
    filename: str | None = None,
) -> IngestResultView:
    """Ingest a document (the raw request body) under `key`, replacing an earlier version.
    Labels and trust come from the request, never from the document's content."""
    data = await _read_body(request)
    return await platform.ingest(
        caller,
        name,
        key=key,
        data=data,
        filename=filename or key.rsplit("/", 1)[-1],
        labels=tuple(labels or ()),
        trust=trust,
    )


@api.delete(
    "/knowledge/collections/{name}/documents/{key:path}", tags=["knowledge"], status_code=204
)
async def delete_document(name: str, key: str, platform: Plat, caller: Caller) -> Response:
    await platform.delete_source(caller, name, key)
    return Response(status_code=204)


@api.patch("/knowledge/collections/{name}/documents/{key:path}", tags=["knowledge"])
async def relabel_document(
    name: str, key: str, spec: LabelsInput, platform: Plat, caller: Caller
) -> SourceView:
    return await platform.relabel(caller, name, key, spec.labels)


@api.post("/knowledge/collections/{name}/sync", tags=["knowledge"])
async def sync(name: str, spec: SyncInput, platform: Plat, caller: Caller) -> SyncResultView:
    """Ingest a directory inside the workspace's configured sync roots (admin)."""
    return await platform.sync(caller, name, spec)


@api.post("/knowledge/search", tags=["knowledge"])
async def search(spec: SearchInput, platform: Plat, caller: Caller) -> SearchResultView:
    return await platform.search(caller, spec)


@api.post("/knowledge/reconcile", tags=["knowledge"])
async def reconcile(platform: Plat, caller: Caller) -> dict[str, int]:
    return await platform.reconcile_knowledge(caller)


# memory


@api.get("/memory", tags=["memory"])
async def memories(
    platform: Plat,
    caller: Caller,
    scope: str | None = None,
    kind: MemoryKind | None = None,
    status: str | None = None,
    query: Annotated[str | None, Query(max_length=500)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[MemoryView]:
    return await platform.memories(
        caller, scope=scope, kind=kind, status=status, query=query, limit=limit
    )


@api.get("/memory/scopes", tags=["memory"])
async def memory_scopes(platform: Plat, caller: Caller) -> list[str]:
    return await platform.memory_scopes(caller)


@api.post("/memory", tags=["memory"], status_code=201)
async def remember(spec: RememberInput, platform: Plat, caller: Caller) -> WriteResultView:
    return await platform.remember(caller, spec)


@api.post("/memory/maintenance", tags=["memory"])
async def maintain(platform: Plat, caller: Caller) -> MaintenanceView:
    return await platform.maintain_memory(caller)


@api.get("/memory/{memory_id}", tags=["memory"])
async def memory(memory_id: str, platform: Plat, caller: Caller) -> MemoryDetail:
    return await platform.memory(caller, memory_id)


@api.post("/memory/{memory_id}/retract", tags=["memory"])
async def retract(memory_id: str, spec: ForgetInput, platform: Plat, caller: Caller) -> MemoryView:
    """Mark a memory as retracted: kept for history, no longer recalled."""
    return await platform.retract_memory(caller, memory_id, spec)


@api.post("/memory/{memory_id}/forget", tags=["memory"], status_code=204)
async def forget(memory_id: str, spec: ForgetInput, platform: Plat, caller: Caller) -> Response:
    """Delete a memory's content from the memory store. Copies in run event logs remain."""
    await platform.forget_memory(caller, memory_id, spec)
    return Response(status_code=204)


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise StarletteHTTPException(413, f"documents are limited to {MAX_UPLOAD_BYTES} bytes")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_UPLOAD_BYTES:
            raise StarletteHTTPException(413, f"documents are limited to {MAX_UPLOAD_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


# --- the web application ----------------------------------------------------------------------


def _spa(path: str) -> Response:
    """Static files of the built web app; any other non-API path gets index.html (client-side
    routing)."""
    if path == "v1" or path.startswith("v1/"):
        raise NotFound(f"no API route /{path}")
    root = STATIC_DIR.resolve()
    if path:
        candidate = (root / path).resolve()
        if candidate.is_relative_to(root) and candidate.is_file():
            immutable = path.startswith("assets/")
            cache = "public, max-age=31536000, immutable" if immutable else "no-cache"
            return FileResponse(candidate, headers={"cache-control": cache})
    index = root / "index.html"
    if index.is_file():
        return FileResponse(index, headers={"cache-control": "no-cache"})
    return HTMLResponse(NOT_BUILT, headers={"cache-control": "no-cache"})


# --- middleware -------------------------------------------------------------------------------


class SecurityHeaders:
    """Adds security headers to every response and refuses unexpected Host headers."""

    def __init__(self, app: ASGIApp, *, allowed_hosts: frozenset[str] | None) -> None:
        self._app = app
        self._allowed = allowed_hosts  # None: any host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        if self._allowed is not None:
            host = _hostname(dict(scope["headers"]).get(b"host", b"").decode("latin-1"))
            if host not in self._allowed:
                response = problem(400, "bad_host", "Bad request", "unexpected Host header")
                await response(scope, receive, send)
                return
        api_path = str(scope.get("path", "")).startswith("/v1")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    headers.setdefault(name, value)
                if api_path:
                    headers.setdefault("cache-control", "no-store")
            await send(message)

        await self._app(scope, receive, send_with_headers)


def _hostname(host: str) -> str:
    """The host part of a Host header value, without port or IPv6 brackets."""
    if host.startswith("["):
        return host[1 : host.find("]")].lower() if "]" in host else host.lower()
    return host.rsplit(":", 1)[0].lower() if host.count(":") == 1 else host.lower()


def allowed_hosts(bind_host: str, extra: tuple[str, ...]) -> frozenset[str] | None:
    """Loopback names, the bind address (unless a wildcard) and configured extras. `*` in the
    extras allows any host (for deployments behind a proxy that validates Host)."""
    if "*" in extra:
        return None
    hosts = set(LOOPBACK_HOSTS) | {h.lower() for h in extra}
    if bind_host not in ("0.0.0.0", "::", ""):  # noqa: S104  # comparing, not binding
        hosts.add(bind_host.lower().strip("[]"))
    return frozenset(hosts)


# --- the application --------------------------------------------------------------------------

type OnReady = Callable[[Platform], Awaitable[None]]


def create_app(
    *,
    platform: Platform | None = None,
    root: Path | None = None,
    on_ready: OnReady | None = None,
    on_stop: OnReady | None = None,
    hosts: frozenset[str] | None = frozenset(LOOPBACK_HOSTS),
) -> FastAPI:
    """The ASGI application. Give an open `platform` (its lifetime stays the caller's), or a
    workspace `root` to open at startup and close at shutdown, when interrupted runs are left
    resumable and resumed by `recover` on the next start."""
    if (platform is None) == (root is None):
        raise ValueError("give either platform or root")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        owned = platform is None
        opened = platform if platform is not None else await Platform.open(cast(Path, root))
        app.state.platform = opened
        try:
            if owned:
                resumed = await opened.ws.recover()
                if resumed:
                    log.info("resumed %d interrupted runs", len(resumed))
            if on_ready is not None:
                await on_ready(opened)
            yield
        finally:
            app.state.platform = None
            try:
                if on_stop is not None:
                    await on_stop(opened)
            finally:
                if owned:
                    await opened.close()

    app = FastAPI(
        title="Hollis API",
        version=API_VERSION,
        summary=f"Hollis {__version__}: agents, runs, approvals, tools, models, knowledge, memory.",
        openapi_url="/v1/openapi.json",
        docs_url=None,  # the bundled docs pages load scripts from a CDN, which the CSP forbids
        redoc_url=None,
        lifespan=lifespan,
    )
    if platform is not None:
        app.state.platform = platform
    app.include_router(api)
    app.add_api_route("/{path:path}", _spa, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_exception_handler(PlatformError, _platform_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unexpected)
    app.add_middleware(SecurityHeaders, allowed_hosts=hosts)
    return app
