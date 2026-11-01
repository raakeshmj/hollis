"""The Hollis API contract: request and view models shared by the REST API, the SDK (embedded and
remote) and, through OpenAPI, the web application (ADR-0027).

Views are computed by the platform from the runtime's own state, so no interface re-derives run
semantics. Compatibility: within API version 1, fields are only added (with defaults), never
removed or repurposed.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from hollis.runtime.context import CompactionSummary, ContextPolicy, TokenAccounting
from hollis.runtime.grounding import GroundingReport
from hollis.runtime.knowledge import KnowledgeAccess, RetrievedPassage, SourceTrust
from hollis.runtime.memory import Certainty, MemoryAccess, MemoryKind, RecalledMemory
from hollis.runtime.policy import RunBudget

API_VERSION = "1"
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


class _View(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- identity ------------------------------------------------------------------------------------


class Role(StrEnum):
    ADMIN = "admin"
    """Everything, including configuration, principals and tokens."""
    OPERATOR = "operator"
    """Run agents, manage own runs, approve (with the approve permission), manage own memory."""
    VIEWER = "viewer"
    """Read what the principal's grants and the run visibility setting allow."""


class PrincipalView(_View):
    name: str
    role: Role
    can_approve: bool
    memory: MemoryAccess | None = None
    """Memory scopes the principal may use; None with admin means all."""
    knowledge: KnowledgeAccess | None = None
    created_at: AwareDatetime


class PrincipalInput(_View):
    name: str = Field(pattern=NAME_PATTERN)
    role: Role = Role.OPERATOR
    can_approve: bool = False
    memory: MemoryAccess | None = None
    knowledge: KnowledgeAccess | None = None


class TokenView(_View):
    token_id: str
    principal: str
    label: str
    created_at: AwareDatetime
    last_used_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None


class CreatedToken(TokenView):
    token: str
    """Shown once; only its hash is stored."""


class TokenInput(_View):
    principal: str
    label: str = Field(default="", max_length=80)
    expires_in_days: float | None = Field(default=None, gt=0, le=3650)


class Session(_View):
    principal: PrincipalView
    expires_at: AwareDatetime


# --- health and settings -------------------------------------------------------------------------


class StorageView(_View):
    name: str
    path: str
    bytes: int


class Health(_View):
    status: Literal["ok", "degraded"]
    version: str
    api_version: str = API_VERSION
    workspace: str
    workspace_path: str
    environment: str
    demo_model: bool
    storage: tuple[StorageView, ...]
    deployments: int
    deployment_problems: int
    active_runs: int
    pending_approvals: int
    problems: tuple[str, ...] = ()


class SettingsView(_View):
    workspace: str
    workspace_path: str
    config_path: str
    config: dict[str, JsonValue]
    """The workspace configuration as stored (credentials appear only as env-var names)."""
    server: dict[str, JsonValue]
    auth: dict[str, JsonValue]


# --- models ---------------------------------------------------------------------------------------


class CredentialView(_View):
    env: str
    """Environment variable name; the value is never returned."""
    present: bool


class DeploymentView(_View):
    id: str
    provider: str
    protocol: str
    model: str
    endpoint: str | None
    locality: str
    tasks: tuple[str, ...]
    capabilities: dict[str, JsonValue]
    credential: CredentialView | None = None
    builtin: bool = False
    status: Literal["ready", "missing_credential", "blocked_by_routing", "invalid"] = "ready"
    status_detail: str = ""
    description: str = ""


class DeploymentInput(_View):
    """A gateway catalog entry (same schema as `[[deployments]]` in hollis.toml)."""

    deployment: dict[str, JsonValue]


class ProbeResult(_View):
    deployment: str
    ok: bool
    latency_ms: float | None = None
    detail: str
    served_model: str | None = None


# --- tools ----------------------------------------------------------------------------------------


class ToolView(_View):
    name: str
    description: str
    version: str
    effect: str
    trust: str
    idempotent: bool
    reconcilable: bool
    isolation: str
    timeout_s: float
    input_schema: dict[str, JsonValue]
    source: str
    """builtin:<group> or mcp:<server>."""
    policy: str
    """Decision under the workspace policy for this environment: allow, require_approval, deny."""
    policy_rule: str | None = None
    policy_reason: str = ""


class McpServerView(_View):
    name: str
    transport: str
    target: str
    """Exact command line (stdio) or endpoint origin (HTTP)."""
    allowlist: tuple[str, ...]
    status: Literal["not_connected", "connected", "error"]
    tools: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    error: str | None = None
    connected_at: AwareDatetime | None = None


class ToolActivity(_View):
    run_id: UUID
    agent: str | None
    invocation_id: str
    tool: str
    decision: str | None
    outcome: str | None
    approver: str | None = None
    at: AwareDatetime


# --- agents ---------------------------------------------------------------------------------------


class AgentInput(_View):
    name: str = Field(pattern=NAME_PATTERN)
    description: str = Field(default="", max_length=500)
    instructions: str = Field(default="", max_length=50_000)
    model: str = Field(min_length=1)
    tools: tuple[str, ...] = ()
    memory: MemoryAccess | None = None
    knowledge: KnowledgeAccess | None = None
    budget: RunBudget | None = None
    """None uses the workspace default output budget and runtime defaults."""
    context: ContextPolicy = ContextPolicy()
    record_episodes: bool = False


class Agent(AgentInput):
    created_at: AwareDatetime
    updated_at: AwareDatetime
    created_by: str
    version: int = Field(ge=1)


# --- runs -----------------------------------------------------------------------------------------


class StartRun(_View):
    input: str = Field(min_length=1, max_length=200_000)
    agent: str | None = None
    model: str | None = None
    """For ad-hoc runs without an agent: a deployment id."""
    instructions: str | None = None
    tools: tuple[str, ...] = ()
    session: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    """Runs in one session share the memory scope session:<principal>/<session>."""


class RunPhase(StrEnum):
    """Run status as shown to users (the runtime's statuses, simplified)."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    """Not terminal, not running: the process stopped. Resume continues it."""


class UsageView(_View):
    input_tokens: int = 0
    output_tokens: int = 0


class RunSummary(_View):
    run_id: UUID
    phase: RunPhase
    status: str
    """The runtime status (pending, model_call_in_flight, tool_calls, ...)."""
    agent: str | None = None
    created_by: str | None = None
    session: str | None = None
    model: str
    provider: str
    protocol: str
    input_preview: str
    created_at: AwareDatetime
    updated_at: AwareDatetime
    awaiting_approvals: int = 0
    turns: int = 0
    tool_calls: int = 0
    usage: UsageView = UsageView()
    error: str | None = None
    duration_s: float = 0.0


class Page[T](BaseModel):
    model_config = ConfigDict(frozen=True)
    items: list[T]
    total: int


class AttemptView(_View):
    attempt: int
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    outcome: Literal["responded", "failed", "in_flight"]
    failure_kind: str | None = None
    message: str | None = None


class ContextView(_View):
    estimate: int
    accounting: TokenAccounting
    budget: int | None
    window: int | None
    history_start: int
    cleared: int
    memories: int
    passages: int
    excluded: tuple[str, ...] = ()


class ModelCallView(_View):
    operation_id: str
    attempts: tuple[AttemptView, ...]
    text: str | None = None
    stop_reason: str | None = None
    usage: UsageView | None = None
    served_model: str | None = None
    context: ContextView | None = None
    duration_ms: int | None = None


class ApprovalRequestView(_View):
    binding: str
    expires_at: AwareDatetime
    reason: str


class ToolCallView(_View):
    invocation_id: str
    turn: int
    index: int
    tool: str
    arguments: JsonValue = None
    raw_arguments: str | None = None
    status: Literal[
        "deciding", "awaiting_approval", "denied", "invalid", "queued", "running",
        "succeeded", "failed", "timed_out", "ambiguous",
    ]  # fmt: skip
    decision: str | None = None
    reason: str | None = None
    rule: str | None = None
    approved: bool | None = None
    approver: str | None = None
    approval_reason: str | None = None
    approval: ApprovalRequestView | None = None
    effect: str | None = None
    trust: str | None = None
    attempts: int = 0
    outcome: str | None = None
    result: str | None = None
    result_truncated: bool = False
    result_chars: int = 0
    artifact_id: str | None = None
    duration_ms: int | None = None
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None


class TurnView(_View):
    index: int
    model_call: ModelCallView
    tool_calls: tuple[ToolCallView, ...] = ()


class CompactionView(_View):
    index: int
    through_turn: int
    method: str
    items: int
    summary: CompactionSummary


class FailureView(_View):
    reason: str
    message: str
    kind: str | None = None


class CancellationView(_View):
    by: str
    reason: str
    interrupted: tuple[str, ...] = ()


class RunDetail(_View):
    summary: RunSummary
    input: str
    instructions: str | None
    budget: RunBudget
    context_policy: ContextPolicy
    tools: tuple[str, ...]
    memory_access: MemoryAccess | None
    knowledge_access: KnowledgeAccess | None
    memories: tuple[RecalledMemory, ...]
    passages: tuple[RetrievedPassage, ...]
    grounding: GroundingReport | None
    compactions: tuple[CompactionView, ...]
    turns: tuple[TurnView, ...]
    answer: str | None
    failure: FailureView | None
    cancellation: CancellationView | None
    deadline: AwareDatetime | None


class EventView(_View):
    sequence: int
    type: str
    occurred_at: AwareDatetime
    summary: str
    payload: dict[str, JsonValue]


class Approval(_View):
    run_id: UUID
    agent: str | None
    invocation_id: str
    tool: str
    effect: str
    trust: str
    idempotent: bool
    arguments: JsonValue
    reason: str
    binding: str
    expires_at: AwareDatetime
    requested_at: AwareDatetime | None = None
    run_input: str


class ApprovalDecisionInput(_View):
    approved: bool
    reason: str | None = Field(default=None, max_length=2000)


class CancelInput(_View):
    reason: str = Field(default="", max_length=2000)


# --- artifacts ------------------------------------------------------------------------------------


class ArtifactView(_View):
    artifact_id: str
    run_id: UUID
    chars: int
    media_type: str
    source: str
    trust: str
    offset: int
    text: str
    truncated: bool


# --- knowledge ------------------------------------------------------------------------------------


class CollectionView(_View):
    name: str
    description: str
    embedding: str | None
    """Embedding model of the collection's space, or None for lexical-only."""
    sources: int
    chunks: int
    created_at: AwareDatetime


class CollectionInput(_View):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    description: str = Field(default="", max_length=500)
    embedded: bool = True


class SourceView(_View):
    source_id: str
    collection: str
    key: str
    title: str
    media_type: str
    version: int
    labels: tuple[str, ...]
    trust: SourceTrust
    effective_date: AwareDatetime | None
    ingested_at: AwareDatetime
    size_bytes: int
    chunks: int
    flagged_chunks: int
    extraction: dict[str, str]


class SectionView(_View):
    index: int
    title: str
    level: int
    path: tuple[str, ...]
    chunks: int


class ChunkView(_View):
    chunk_id: str
    ordinal: int
    section: str
    location: str
    text: str
    flags: tuple[str, ...]
    tokens: int


class SourceDetail(_View):
    source: SourceView
    sections: tuple[SectionView, ...]
    chunks: tuple[ChunkView, ...]


class IngestResultView(_View):
    action: str
    source: SourceView | None = None
    reason: str = ""


class SyncInput(_View):
    path: str
    labels: tuple[str, ...] = ()
    trust: SourceTrust = SourceTrust.INTERNAL


class SyncResultView(_View):
    created: tuple[str, ...]
    updated: tuple[str, ...]
    unchanged: tuple[str, ...]
    deleted: tuple[str, ...]
    rejected: tuple[tuple[str, str], ...]


class LabelsInput(_View):
    labels: tuple[str, ...]


class SearchInput(_View):
    query: str = Field(min_length=1, max_length=2000)
    collections: tuple[str, ...] = ()
    limit: int = Field(default=8, ge=1, le=50)


class SearchHit(_View):
    passage: RetrievedPassage
    source_key: str


class SearchResultView(_View):
    query: str
    hits: tuple[SearchHit, ...]
    notes: tuple[str, ...] = ()
    elapsed_ms: float


# --- memory ---------------------------------------------------------------------------------------


class MemoryView(_View):
    memory_id: str
    scope: str
    kind: MemoryKind
    content: str
    subject: str | None
    key: str | None
    certainty: Certainty
    status: str
    status_reason: str | None
    source: str
    external: bool
    recorded_at: AwareDatetime
    valid_from: AwareDatetime | None
    valid_until: AwareDatetime | None
    supersedes: str | None
    superseded_by: str | None
    conflicts_with: tuple[str, ...]
    tags: tuple[str, ...]
    reinforced: int
    recall_count: int
    last_recalled_at: AwareDatetime | None


class MemoryChangeView(_View):
    at: AwareDatetime
    action: str
    actor: str
    detail: str


class MemoryDetail(_View):
    memory: MemoryView
    history: tuple[MemoryChangeView, ...]


class RememberInput(_View):
    scope: str
    content: str = Field(min_length=1, max_length=4000)
    kind: MemoryKind = MemoryKind.FACT
    subject: str | None = None
    key: str | None = None
    change: Literal["new", "update", "correction", "temporary"] = "new"
    valid_until: datetime | None = None


class WriteResultView(_View):
    action: str
    memory: MemoryView | None = None
    affected: tuple[str, ...] = ()
    reason: str = ""


class ForgetInput(_View):
    reason: str = Field(min_length=1, max_length=500)


class MaintenanceView(_View):
    expired: tuple[str, ...]
    deleted: tuple[str, ...]


def jsonable(value: Any) -> JsonValue:
    """A JSON-safe copy of a pydantic model or plain value (for payload views)."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value
