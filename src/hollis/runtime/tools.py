"""The tool port: what the runtime needs to know about tools, policy and approval (ADR-0014).

Concrete tools, registries, policies and protocol integrations live in `hollis.tools`; the
runtime depends only on these types and protocols. Everything here is deterministic application
logic. Nothing a model says, and nothing in a tool's description, grants permission.
"""

import fnmatch
import hashlib
import json
import posixpath
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from hollis.runtime.conversation import TOOL_NAME_PATTERN, ToolCall, ToolSpec
from hollis.runtime.knowledge import KnowledgeAccess
from hollis.runtime.memory import MemoryAccess


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolEffect(StrEnum):
    """Declared by the registering code (trusted), never by a tool description or a server."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"


class ToolTrust(StrEnum):
    TRUSTED = "trusted"
    """First-party code registered by the operator."""
    EXTERNAL = "external"
    """Definitions or results from outside the process (e.g. MCP servers). Results are framed as
    untrusted data when sent to the model."""


class Isolation(StrEnum):
    """What actually separates tool execution from the Hollis process. Ordered weakest first."""

    NONE = "none"
    """Runs inside the Hollis process (or a thread). Not a sandbox."""
    PROCESS = "process"
    """A separate OS process with a scrubbed environment and resource limits. Not a sandbox:
    it has the same user, filesystem and network access as Hollis unless the OS restricts it."""
    SANDBOX = "sandbox"
    """A verified isolation boundary. No M3 backend provides this."""

    @property
    def rank(self) -> int:
        return list(Isolation).index(self)


class ToolDescriptor(_Frozen):
    name: str = Field(pattern=TOOL_NAME_PATTERN)
    version: str = Field(min_length=1)
    description: str
    input_schema: dict[str, JsonValue]
    effect: ToolEffect
    idempotent: bool = False
    """True only if repeating the same invocation cannot cause an additional side effect. Enables
    re-execution when an attempt's outcome is unknown."""
    reconcilable: bool = False
    """True if the tool can look up the outcome of an earlier attempt (`Tool.reconcile`)."""
    trust: ToolTrust = ToolTrust.TRUSTED
    timeout_s: float = Field(default=30.0, gt=0, le=3600)
    isolation: Isolation = Isolation.NONE

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name, description=self.description, input_schema=self.input_schema
        )

    @property
    def schema_digest(self) -> str:
        """Digest of everything the model sees plus the version, to detect contract changes."""
        return digest(
            {
                "name": self.name,
                "version": self.version,
                "description": self.description,
                "input_schema": self.input_schema,
            }
        )

    @property
    def ref(self) -> "ToolRef":
        return ToolRef(
            name=self.name,
            version=self.version,
            schema_digest=self.schema_digest,
            effect=self.effect,
            idempotent=self.idempotent,
            reconcilable=self.reconcilable,
            trust=self.trust,
        )


class ToolRef(_Frozen):
    """A tool's identity and declared behaviour, recorded in RunCreated."""

    name: str
    version: str
    schema_digest: str
    effect: ToolEffect
    idempotent: bool
    reconcilable: bool = False
    trust: ToolTrust


class ToolArgumentError(ValueError):
    """Arguments do not satisfy the tool's input contract. The message is returned to the model."""


class ToolError(Exception):
    """A tool reports a failure. The message is returned to the model, so keep it free of
    secrets and infrastructure detail."""


class TextPart(_Frozen):
    type: Literal["text"] = "text"
    text: str


class JsonPart(_Frozen):
    type: Literal["json"] = "json"
    data: JsonValue


class ReferencePart(_Frozen):
    """Content not inlined (images, files, large blobs): described, not embedded."""

    type: Literal["reference"] = "reference"
    kind: str
    uri: str | None = None
    media_type: str | None = None
    size_bytes: int | None = None
    description: str | None = None


ResultPart = TextPart | JsonPart | ReferencePart


class ToolOutput(_Frozen):
    """What a tool returns. The runtime limits, redacts and frames it before recording."""

    parts: tuple[ResultPart, ...] = ()
    is_error: bool = False

    @classmethod
    def text(cls, text: str, *, is_error: bool = False) -> "ToolOutput":
        return cls(parts=(TextPart(text=text),), is_error=is_error)


class ToolContext(_Frozen):
    run_id: UUID
    invocation_id: str
    """Stable across attempts: use it as an idempotency key with external services."""
    attempt: int
    deadline: AwareDatetime | None = None
    memory: MemoryAccess | None = None
    """Memory namespaces granted to the run (from RunCreated), for memory tools."""
    knowledge: KnowledgeAccess | None = None
    """Knowledge collections and labels granted to the run, for knowledge tools."""


@runtime_checkable
class Tool(Protocol):
    @property
    def descriptor(self) -> ToolDescriptor: ...

    def validate(self, arguments: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        """Return normalized arguments, or raise ToolArgumentError."""
        ...

    async def invoke(self, arguments: dict[str, JsonValue], context: ToolContext) -> ToolOutput: ...

    async def reconcile(self, context: ToolContext) -> ToolOutput | None:
        """Outcome of an earlier attempt of this invocation if it can be determined, else None.
        Only called when `descriptor.reconcilable` is True."""
        ...


class ToolCatalog(Protocol):
    def get(self, name: str) -> Tool | None: ...


# --- policy -----------------------------------------------------------------------------------


class Decision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"
    INVALID = "invalid"
    """Not a policy outcome: the call itself was malformed, unknown or over a limit."""


class PolicyRequest(_Frozen):
    run_id: UUID
    invocation_id: str
    tool: ToolRef
    arguments: dict[str, JsonValue]
    environment: str | None = None
    """Operator-defined execution environment label (e.g. "dev", "prod")."""
    depth: int = Field(default=0, ge=0)
    """Delegation depth of the run (0: nobody delegated it), recorded in its lineage."""


class PolicyDecision(_Frozen):
    decision: Literal[Decision.ALLOW, Decision.REQUIRE_APPROVAL, Decision.DENY]
    reason: str
    rule: str | None = None


class ToolPolicy(Protocol):
    def decide(self, request: PolicyRequest) -> PolicyDecision: ...


_STRICTNESS = {Decision.ALLOW: 0, Decision.REQUIRE_APPROVAL: 1, Decision.DENY: 2}


class PolicyOverlayRule(_Frozen):
    """A per-run rule recorded with the run that can only make a policy decision stricter
    (ADR-0035): an agent can be held to less than the workspace allows, never more.

    It matches a tool call by tool name (glob) and declared effect, and optionally by
    arguments: `outside` maps an argument name to allowed path globs, and the rule matches when
    any value of that argument (a string or a list of strings) is outside all of them, or tries
    to leave the root with `..`. Paths are compared relative, as the tools receive them."""

    decision: Literal[Decision.REQUIRE_APPROVAL, Decision.DENY]
    tools: str = Field(default="*", min_length=1, max_length=200)
    effects: tuple[ToolEffect, ...] = ()
    outside: dict[str, tuple[str, ...]] = Field(default_factory=dict[str, tuple[str, ...]])
    name: str = Field(default="", max_length=80)
    reason: str = Field(default="", max_length=300)

    def matches(self, request: PolicyRequest) -> bool:
        tool = request.tool
        if not fnmatch.fnmatchcase(tool.name, self.tools):
            return False
        if self.effects and tool.effect not in self.effects:
            return False
        if not self.outside:
            return True
        return any(
            _outside(request.arguments.get(argument), allowed)
            for argument, allowed in self.outside.items()
        )


def _outside(value: JsonValue, allowed: Sequence[str]) -> bool:
    values = value if isinstance(value, list) else [value]
    for item in values:
        if not isinstance(item, str):
            continue
        normalized = posixpath.normpath(item.replace("\\", "/").removeprefix("./"))
        if normalized.startswith(("..", "/")) or not any(
            fnmatch.fnmatchcase(normalized, pattern) for pattern in allowed
        ):
            return True
    return False


def apply_overlay(
    decision: PolicyDecision, rules: Sequence[PolicyOverlayRule], request: PolicyRequest
) -> PolicyDecision:
    """The workspace decision, unless a matching overlay rule is stricter (the strictest
    matching rule wins). Never loosens."""
    strictest: tuple[int, PolicyOverlayRule] | None = None
    for index, rule in enumerate(rules):
        if rule.matches(request) and (
            strictest is None or _STRICTNESS[rule.decision] > _STRICTNESS[strictest[1].decision]
        ):
            strictest = (index, rule)
    if strictest is None or _STRICTNESS[strictest[1].decision] <= _STRICTNESS[decision.decision]:
        return decision
    index, rule = strictest
    return PolicyDecision(
        decision=rule.decision,
        reason=rule.reason or f"the agent's policy rule {rule.name or index} applies",
        rule=f"agent:{rule.name or index}",
    )


class DefaultToolPolicy:
    """Fail-safe default: trusted read-only tools run; everything else needs approval."""

    def decide(self, request: PolicyRequest) -> PolicyDecision:
        tool = request.tool
        if tool.effect is ToolEffect.READ_ONLY and tool.trust is ToolTrust.TRUSTED:
            return PolicyDecision(
                decision=Decision.ALLOW, reason="trusted read-only tool", rule="default"
            )
        return PolicyDecision(
            decision=Decision.REQUIRE_APPROVAL,
            reason=f"{tool.trust.value} {tool.effect.value} tool requires approval",
            rule="default",
        )


# --- approval ---------------------------------------------------------------------------------


class ApprovalRequest(_Frozen):
    run_id: UUID
    invocation_id: str
    tool: ToolRef
    arguments: dict[str, JsonValue]
    reason: str
    """Why approval is required (from policy)."""
    binding: str
    """Digest over the run, invocation, tool identity and exact arguments. An approval is valid
    only for this binding."""
    expires_at: datetime


class ApprovalDecision(_Frozen):
    binding: str
    approved: bool
    approver: str = Field(min_length=1)
    reason: str | None = None


class Approver(Protocol):
    async def review(self, request: ApprovalRequest) -> ApprovalDecision | None:
        """Decide now, or return None to leave the request pending (the run suspends)."""
        ...


def approval_binding(run_id: UUID, invocation_id: str, tool: ToolRef, arguments: object) -> str:
    return digest(
        {
            "run_id": str(run_id),
            "invocation_id": invocation_id,
            "tool": tool.name,
            "version": tool.version,
            "schema_digest": tool.schema_digest,
            "arguments": arguments,
        }
    )


def invocation_id_for(run_id: UUID, turn: int, index: int) -> str:
    return f"{run_id}/turn/{turn}/call/{index}"


def digest(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def call_arguments(call: ToolCall) -> dict[str, JsonValue]:
    return dict(call.arguments) if call.arguments is not None else {}
