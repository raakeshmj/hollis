"""The Hollis API contract: request and view models shared by the REST API, the SDK (embedded and
remote) and, through OpenAPI, the web application (ADR-0027).

Views are computed by the platform from the runtime's own state, so no interface re-derives run
semantics. Compatibility: within API version 1, fields are only added (with defaults), never
removed or repurposed.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, model_validator

from hollis.runtime.context import CompactionSummary, ContextPolicy, TokenAccounting
from hollis.runtime.delegation import Assignment, DelegationLimits, DelegationOverrides
from hollis.runtime.grounding import GroundingReport
from hollis.runtime.knowledge import KnowledgeAccess, RetrievedPassage, SourceTrust
from hollis.runtime.memory import Certainty, MemoryAccess, MemoryKind, RecalledMemory
from hollis.runtime.policy import RunBudget
from hollis.runtime.tools import PolicyOverlayRule

API_VERSION = "1"
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


class _View(BaseModel):
    """A response. Unknown fields are ignored so older clients keep working when a newer server
    adds fields (nested runtime models are still strict: keep SDK and server versions close)."""

    model_config = ConfigDict(frozen=True, extra="ignore")


class _Input(BaseModel):
    """A request. Unknown fields are rejected."""

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


class PrincipalInput(_Input):
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


class TokenInput(_Input):
    principal: str
    label: str = Field(default="", max_length=80)
    expires_in_days: float | None = Field(default=None, gt=0, le=3650)


class Session(_View):
    principal: PrincipalView
    expires_at: AwareDatetime | None = None
    """None when the caller authenticated with an API token rather than a browser session."""


class LoginInput(_Input):
    token: str = Field(min_length=8, max_length=200)
    """An API token or a single-use login token (from `hollis serve` or `hollis token login`)."""


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


class DeploymentInput(_Input):
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


class AgentInput(_Input):
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
    delegates: tuple[str, ...] = Field(default=(), max_length=16)
    """Agents this agent may delegate bounded tasks to (one `delegate_<agent>` tool each)."""
    delegation: DelegationOverrides | None = None
    """Narrows the workspace's delegation limits for runs of this agent and below; fields not
    set inherit them."""
    policy: tuple[PolicyOverlayRule, ...] = Field(default=(), max_length=32)
    """Overlay rules that make the workspace's tool policy stricter for this agent's runs and
    every run they delegate (ADR-0035). They can never allow more."""
    contract: str | None = Field(default=None, pattern=NAME_PATTERN)
    """An output contract: runs get a `submit_<contract>` tool, and the submission accepted
    after checks against the run's log is the run's structured output (ADR-0034)."""
    skills: tuple[str, ...] = Field(default=(), max_length=32)
    """Workspace Agent Skills offered to the agent by name and description (ADR-0033)."""


class AgentOrigin(_View):
    """The template an agent was made from (ADR-0033)."""

    template: str
    version: str
    digest: str
    parameters: dict[str, str] = {}
    overrides: tuple[str, ...] = ()
    """Template-determined fields that were customized; upgrades keep them."""


class Agent(AgentInput):
    model_config = ConfigDict(frozen=True, extra="ignore")

    created_at: AwareDatetime
    updated_at: AwareDatetime
    created_by: str
    version: int = Field(ge=1)
    origin: AgentOrigin | None = None
    """Set when the agent was made from a template."""


# --- specialization -------------------------------------------------------------------------------


class RequirementCheck(_View):
    requirement: str
    ok: bool
    hard: bool
    """A failed hard check blocks creating an agent; a soft one is a warning."""
    detail: str


class DeploymentFit(_View):
    deployment: str
    ok: bool
    checks: tuple[RequirementCheck, ...]


class ParameterView(_View):
    name: str
    description: str
    default: str | None
    choices: tuple[str, ...] = ()
    pattern: str | None = None


class CapabilityView(_View):
    id: str
    version: str
    name: str
    description: str
    tools: tuple[str, ...]
    optional_tools: tuple[str, ...]
    configured_commands: bool
    guidance: str
    policy: tuple[PolicyOverlayRule, ...]
    available: bool
    """Every required tool exists in this workspace."""


class TemplateView(_View):
    id: str
    version: str
    name: str
    description: str
    source: str
    """builtin, or workspace:<file name>."""
    digest: str
    capabilities: tuple[str, ...]
    contract: str | None
    parameters: tuple[ParameterView, ...]
    examples: tuple[str, ...]
    ready: bool
    """No hard workspace requirement fails (the model is checked per deployment)."""
    checks: tuple[RequirementCheck, ...]
    agents: tuple[str, ...] = ()
    """Agents made from this template."""


class TemplateDetail(TemplateView):
    instructions: str
    """With default parameter values."""
    tools: tuple[str, ...]
    """What an agent made now would get."""
    policy: tuple[PolicyOverlayRule, ...]
    budget: RunBudget | None
    skills: tuple[str, ...]
    requires: dict[str, JsonValue]
    fits: tuple[DeploymentFit, ...]
    """Each text deployment the routing policy allows, checked against the model needs."""


class ContractView(_View):
    id: str
    version: str
    name: str
    description: str
    rules: tuple[str, ...]
    tool: str
    input_schema: dict[str, JsonValue]


class SkillView(_View):
    name: str
    description: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = {}
    allowed_tools: tuple[str, ...] = ()
    """As declared by the skill. Hollis grants nothing from it."""
    files: tuple[str, ...] = ()


class SkillsView(_View):
    directory: str | None
    skills: tuple[SkillView, ...]
    problems: tuple[str, ...] = ()
    """Skill folders that could not be loaded, and why."""


class AgentOverrides(_Input):
    """Agent fields to set instead of the template's (or besides it, for fields a template
    never sets). Unset fields are not overridden."""

    description: str | None = Field(default=None, max_length=500)
    instructions: str | None = Field(default=None, max_length=50_000)
    tools: tuple[str, ...] | None = None
    memory: MemoryAccess | None = None
    knowledge: KnowledgeAccess | None = None
    budget: RunBudget | None = None
    context: ContextPolicy | None = None
    record_episodes: bool | None = None
    delegates: tuple[str, ...] | None = Field(default=None, max_length=16)
    delegation: DelegationOverrides | None = None
    policy: tuple[PolicyOverlayRule, ...] | None = Field(default=None, max_length=32)
    skills: tuple[str, ...] | None = Field(default=None, max_length=32)


class FromTemplateInput(_Input):
    template: str = Field(pattern=NAME_PATTERN)
    name: str = Field(pattern=NAME_PATTERN)
    model: str = Field(min_length=1)
    parameters: dict[str, str] = Field(default_factory=dict[str, str], max_length=16)
    overrides: AgentOverrides = AgentOverrides()


class FieldChange(_View):
    field: str
    before: JsonValue
    after: JsonValue


class UpgradePreview(_View):
    agent: str
    template: str
    from_version: str
    to_version: str
    from_digest: str
    to_digest: str
    up_to_date: bool
    changes: tuple[FieldChange, ...]
    """Fields that would change."""
    kept: tuple[str, ...]
    """Customized fields the upgrade keeps."""
    checks: tuple[RequirementCheck, ...] = ()


class UpgradeInput(_Input):
    digest: str = Field(min_length=1, max_length=100)
    """The template digest the preview showed: the upgrade is refused if it changed since."""
    reset: tuple[str, ...] = ()
    """Customized fields to take from the template again."""


class ExportInput(_Input):
    id: str | None = Field(default=None, pattern=NAME_PATTERN)
    """Template id (default: the agent's name)."""
    version: str = Field(default="1.0.0", pattern=r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")


class TemplateExport(_View):
    id: str
    version: str
    filename: str
    content: str
    """The template as TOML: put it in a workspace's templates directory to use it there."""


class OutputView(_View):
    """A run's structured output under its agent's contract (ADR-0034)."""

    contract: str
    status: Literal["accepted", "rejected", "missing"]
    """accepted: a submission passed the checks against the run's log; rejected: submissions
    were made and none passed; missing: none was made."""
    attempts: int
    data: dict[str, JsonValue] | None = None
    invocation_id: str | None = None
    problems: tuple[str, ...] = ()
    """Why the latest rejected submission was refused."""


# --- runs -----------------------------------------------------------------------------------------


STEP_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"


class PlanStep(_Input):
    id: str = Field(pattern=STEP_PATTERN)
    agent: str = Field(pattern=NAME_PATTERN)
    objective: str = Field(min_length=1, max_length=4000)
    context: str = Field(default="", max_length=20_000)
    expected_output: str = Field(default="", max_length=2000)
    depends_on: tuple[str, ...] = Field(default=(), max_length=8)
    """Steps that must complete first; their reports are passed to this step verbatim."""


class Plan(_Input):
    """A fixed delegation plan run by the deterministic `workflow` coordinator (ADR-0030):
    every wave of ready steps is delegated at once, and a step runs only after the steps it
    depends on completed. Steps asked the same objective have their answers compared."""

    steps: tuple[PlanStep, ...] = Field(min_length=1, max_length=32)
    answer: Literal["summary", "last"] = "summary"
    """summary: every step's outcome and answer. last: the answer of the one step nothing
    depends on (a hand-off to that agent)."""

    @model_validator(mode="after")
    def _check(self) -> Self:
        ids = [step.id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("step ids must be unique")
        known = set(ids)
        for step in self.steps:
            unknown = set(step.depends_on) - known
            if unknown:
                raise ValueError(f"step {step.id!r} depends on unknown steps {sorted(unknown)}")
        done: set[str] = set()
        while len(done) < len(ids):  # Kahn's algorithm: every step must become ready
            ready = {s.id for s in self.steps if s.id not in done and set(s.depends_on) <= done}
            if not ready:
                raise ValueError("the plan's dependencies form a cycle")
            done |= ready
        if self.answer == "last" and len(self.sinks) != 1:
            raise ValueError('answer="last" needs exactly one step that nothing depends on')
        return self

    @property
    def sinks(self) -> list[str]:
        needed = {d for step in self.steps for d in step.depends_on}
        return [step.id for step in self.steps if step.id not in needed]


class StartRun(_Input):
    input: str = Field(min_length=1, max_length=200_000)
    agent: str | None = None
    model: str | None = None
    """For ad-hoc runs without an agent: a deployment id."""
    instructions: str | None = None
    tools: tuple[str, ...] = ()
    session: str | None = Field(default=None, pattern=r"^[A-Za-z0-9._-]{1,64}$")
    """Runs in one session share the memory scope session:<principal>/<session>."""
    plan: Plan | None = None
    """Run a fixed plan of delegations instead of one agent (operators and admins)."""


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
    parent_run_id: UUID | None = None
    """The run that delegated this one, if any."""
    root_run_id: UUID | None = None
    depth: int = 0
    children: int = 0
    """Runs this run delegated."""
    objective: str | None = None
    """The assigned objective of a delegated run."""


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


class LineageStep(_View):
    run_id: UUID
    agent: str | None = None
    objective: str | None = None


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
    lineage: tuple[LineageStep, ...] = ()
    """Ancestors of a delegated run, root first."""
    assignment: Assignment | None = None
    delegation: DelegationLimits | None = None
    policy: tuple[PolicyOverlayRule, ...] = ()
    """Overlay rules this run was held to (its agent's and its ancestors')."""
    output: OutputView | None = None


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
    root_run_id: UUID | None = None
    """For a delegated run: the orchestration it belongs to."""
    depth: int = 0
    lineage: tuple[str, ...] = ()
    """Agents from the root to this run (unnamed runs as "run")."""


class ApprovalDecisionInput(_Input):
    approved: bool
    reason: str | None = Field(default=None, max_length=2000)


class CancelInput(_Input):
    reason: str = Field(default="", max_length=2000)


# --- orchestration ------------------------------------------------------------------------------


class VerificationView(_View):
    claims: int = 0
    supported: int = 0
    weak: int = 0
    uncited: int = 0
    fabricated: int = 0


class ReportView(_View):
    """What the delegating run received: Hollis-observed facts plus the child's self-report."""

    outcome: str
    """completed, failed, cancelled, timed_out, budget_exhausted or refused (Hollis-observed)."""
    reason: str | None = None
    status: str | None = None
    """Self-reported: done, partial, blocked or needs_input."""
    confidence: str | None = None
    answer: str | None = None
    """The child's one-line answer as it reported it (model output, unverified)."""
    open_questions: tuple[str, ...] = ()
    verification: VerificationView | None = None
    side_effects_unknown: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    output: OutputView | None = None
    """The child's structured output and whether it passed its contract's checks."""


class OrchestrationNode(_View):
    run: RunSummary
    parent_invocation_id: str | None = None
    inputs: tuple[UUID, ...] = ()
    """Runs whose results were passed to this one (data dependencies)."""
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    report: ReportView | None = None
    narrowed: tuple[str, ...] = ()
    """Access the agent is configured with that this run did not get (permissions narrow)."""
    waiting_on: str | None = None
    """Why the run is not progressing: an approval, a child, or an interruption."""


class RefusalView(_View):
    parent_run_id: UUID
    agent: str
    reason: str


class DisagreementView(_View):
    objective: str
    runs: tuple[UUID, ...]
    answers: tuple[str, ...]


class OrchestrationTotals(_View):
    runs: int
    phases: dict[str, int]
    input_tokens: int
    output_tokens: int
    coordinator_tokens: int
    """Tokens of runs that delegated (coordination overhead)."""
    worker_tokens: int
    wall_s: float
    agent_s: float
    """Summed duration of every delegated run (above wall_s means they overlapped)."""
    max_depth: int
    waiting_approvals: int
    failed: int


class OrchestrationView(_View):
    root_run_id: UUID
    focus_run_id: UUID
    limits: DelegationLimits | None
    nodes: tuple[OrchestrationNode, ...]
    """Pre-order: each run is followed by the runs it delegated."""
    totals: OrchestrationTotals
    refusals: tuple[RefusalView, ...] = ()
    disagreements: tuple[DisagreementView, ...] = ()
    hidden: int = 0
    """Runs in the tree the caller cannot see."""


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


class CollectionInput(_Input):
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


class SyncInput(_Input):
    path: str
    labels: tuple[str, ...] = ()
    trust: SourceTrust = SourceTrust.INTERNAL


class SyncResultView(_View):
    created: tuple[str, ...]
    updated: tuple[str, ...]
    unchanged: tuple[str, ...]
    deleted: tuple[str, ...]
    rejected: tuple[tuple[str, str], ...]


class LabelsInput(_Input):
    labels: tuple[str, ...]


class SearchInput(_Input):
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


class RememberInput(_Input):
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


class ForgetInput(_Input):
    reason: str = Field(min_length=1, max_length=500)


class MaintenanceView(_View):
    expired: tuple[str, ...]
    deleted: tuple[str, ...]


def jsonable(value: Any) -> JsonValue:
    """A JSON-safe copy of a pydantic model or plain value (for payload views)."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value
