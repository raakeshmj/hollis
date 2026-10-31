"""Provider-neutral model types and the ModelProvider port, owned by the runtime core.

Providers translate their own failures into `ModelCallError` with a `FailureKind`. The kind
determines the `CallOutcome` (whether the provider may have generated, and charged for, a
response) and whether a retry can be safe. See ADR-0007.
"""

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from hollis.runtime.conversation import (
    ContextBlock,
    HistoryItem,
    ProviderContinuation,
    ToolCall,
    ToolSpec,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Usage(_Frozen):
    """Token usage as reported by the provider (actual, not estimated)."""

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0)
    thinking_tokens: int | None = Field(default=None, ge=0)
    """Portion of `output_tokens` spent on reasoning, when the provider reports it."""
    cache_included_in_input: bool = False
    """True when `input_tokens` already includes cached tokens (OpenAI-style). Anthropic reports
    cache reads and writes separately from `input_tokens`."""

    @property
    def total_input_tokens(self) -> int:
        """Every input token the provider processed for the request, cached or not."""
        if self.cache_included_in_input:
            return self.input_tokens
        return (
            self.input_tokens
            + (self.cache_read_input_tokens or 0)
            + (self.cache_creation_input_tokens or 0)
        )


class StopReason(StrEnum):
    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    REFUSAL = "refusal"
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"
    CONTENT_FILTERED = "content_filtered"
    TOOL_USE = "tool_use"
    OTHER = "other"


class ModelResponse(_Frozen):
    content: str
    stop_reason: StopReason = StopReason.END_TURN
    usage: Usage | None = None
    served_model: str | None = None
    """Model identifier the provider reports having used."""
    provider_request_id: str | None = None
    """Request identifier from the provider's response headers, for support and correlation."""
    provider_response_id: str | None = None
    """Identifier of the response object itself (e.g. a message or completion id)."""
    omitted_block_types: tuple[str, ...] = ()
    """Non-text content block types present in the provider response but not mapped."""
    tool_calls: tuple[ToolCall, ...] = ()
    continuation: ProviderContinuation | None = None
    """Provider content to send back verbatim when continuing after tool calls."""


class ModelRequest(_Frozen):
    operation_id: str = Field(min_length=1)
    """Idempotency key: identical for every attempt of one logical model call."""
    model: str = Field(min_length=1)
    """The deployment's wire model identifier."""
    input: str
    """The run's initial user message."""
    instructions: str | None = None
    context: tuple[ContextBlock, ...] = ()
    """Reference data (recalled memories, compaction summaries) sent before `input` in the first
    user message, rendered with `render_context`. Never part of `instructions`."""
    history: tuple[HistoryItem, ...] = ()
    """Assistant turns and tool results after `input`, in order. After compaction this starts at
    the first turn kept verbatim."""
    tools: tuple[ToolSpec, ...] = ()
    max_output_tokens: int = Field(ge=1)
    timeout_s: float = Field(gt=0)
    """Wall-clock limit for this physical attempt."""


class CallOutcome(StrEnum):
    NOT_PROCESSED = "not_processed"
    """The provider rejected the request; no response was generated."""
    AMBIGUOUS = "ambiguous"
    """The provider may have generated (and charged for) a response that was not received."""


class FailureKind(StrEnum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    BILLING = "billing"
    INVALID_REQUEST = "invalid_request"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    OVERLOADED = "overloaded"
    UNAVAILABLE = "unavailable"
    CONFLICT = "conflict"
    CONNECTION_FAILED = "connection_failed"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    CONNECTION_LOST = "connection_lost"
    INVALID_RESPONSE = "invalid_response"
    """A response arrived but could not be interpreted; it may have been generated and billed."""
    INTERRUPTED = "interrupted"
    """Assigned by the runtime when an attempt has an intent but no recorded outcome."""

    @property
    def outcome(self) -> CallOutcome:
        return CallOutcome.AMBIGUOUS if self in _AMBIGUOUS else CallOutcome.NOT_PROCESSED

    @property
    def retryable(self) -> bool:
        """Whether retrying can succeed. Ambiguous kinds are additionally subject to policy."""
        return self in _TRANSIENT or self in _AMBIGUOUS


_AMBIGUOUS = frozenset(
    {
        FailureKind.TIMEOUT,
        FailureKind.SERVER_ERROR,
        FailureKind.CONNECTION_LOST,
        FailureKind.INVALID_RESPONSE,
        FailureKind.INTERRUPTED,
    }
)
_TRANSIENT = frozenset(
    {
        FailureKind.RATE_LIMITED,
        FailureKind.OVERLOADED,
        FailureKind.UNAVAILABLE,
        FailureKind.CONFLICT,
        FailureKind.CONNECTION_FAILED,
    }
)


class ModelCallError(Exception):
    """A classified failure of one physical model call.

    `message` must already be safe to persist: providers build it from structured fields and
    must never include credentials. The runtime additionally redacts it before persisting.
    """

    def __init__(
        self,
        kind: FailureKind,
        message: str,
        *,
        provider_request_id: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.provider_request_id = provider_request_id
        self.retry_after_s = retry_after_s

    @property
    def outcome(self) -> CallOutcome:
        return self.kind.outcome


class Locality(StrEnum):
    """Where a deployment's endpoint lives, for routing policy and auditing (ADR-0013)."""

    LOCAL = "local"
    """Loopback on the same machine."""
    PRIVATE = "private"
    """Operator-controlled network: private cloud, enterprise or on-premises."""
    EXTERNAL = "external"
    """A third-party service outside the operator's infrastructure."""


class Task(StrEnum):
    """Model task families. Each family gets its own port; M2 implements text generation."""

    TEXT_GENERATION = "text_generation"


class UsageReporting(StrEnum):
    EXACT = "exact"
    APPROXIMATE = "approximate"
    NONE = "none"


class TextCapabilities(_Frozen):
    """What a deployment supports for text generation. Declared, then validated before calls."""

    system_prompt: bool = True
    max_output_tokens: int | None = Field(default=None, ge=1)
    reasoning_effort: tuple[str, ...] = ()
    """Effort values the deployment accepts; empty means effort must not be sent."""
    usage_reporting: UsageReporting = UsageReporting.EXACT
    structured_output: bool = False
    tools: bool = False
    streaming: bool = False
    image_input: bool = False
    context_window_tokens: int | None = Field(default=None, ge=1)
    """Total tokens (input plus output) the deployment accepts per request. None means unknown:
    the context engine then applies only a configured input budget, and records that the
    window is unknown."""


class ModelCapabilities(_Frozen):
    tasks: frozenset[Task] = frozenset({Task.TEXT_GENERATION})
    text: TextCapabilities | None = TextCapabilities()


class DeploymentRef(_Frozen):
    """The identity of a model deployment, recorded in each run (ADR-0011).

    Recovery refuses to continue a run if the resolved deployment's identity has changed.
    """

    deployment: str = Field(min_length=1)
    """Logical model id that runs reference."""
    provider: str = Field(min_length=1)
    """Informational provider label (e.g. anthropic, vllm, ollama); never selects code."""
    protocol: str = Field(min_length=1)
    model: str = Field(min_length=1)
    """Identifier sent on the wire."""
    endpoint: str | None = None
    """Endpoint origin (scheme://host:port), without path, query or credentials."""
    locality: Locality
    deduplicates_requests: bool
    """True only if repeating a request with the same operation id cannot produce a second
    generation. Determines the default ambiguous-outcome policy."""


class DeploymentDescriptor(DeploymentRef):
    capabilities: ModelCapabilities = ModelCapabilities()

    @property
    def ref(self) -> DeploymentRef:
        return DeploymentRef.model_validate(self.model_dump(exclude={"capabilities"}))


class CapabilityMismatch(ValueError):
    """A run needs something its deployment does not support; nothing was persisted."""


def require_text_generation(
    descriptor: DeploymentDescriptor,
    *,
    has_instructions: bool,
    max_output_tokens: int,
    uses_tools: bool = False,
) -> None:
    caps = descriptor.capabilities
    text = caps.text
    problems: list[str] = []
    if Task.TEXT_GENERATION not in caps.tasks or text is None:
        problems.append("text generation is not supported")
    else:
        if has_instructions and not text.system_prompt:
            problems.append("instructions (system prompt) are not supported")
        if uses_tools and not text.tools:
            problems.append("tool calling is not declared in the deployment's capabilities")
        if text.max_output_tokens is not None and max_output_tokens > text.max_output_tokens:
            problems.append(
                f"max_output_tokens {max_output_tokens} exceeds the limit {text.max_output_tokens}"
            )
    if problems:
        raise CapabilityMismatch(f"deployment {descriptor.deployment!r}: " + "; ".join(problems))


class ModelProvider(Protocol):
    """Text-generation port. One instance serves one deployment."""

    @property
    def descriptor(self) -> DeploymentDescriptor: ...

    async def generate(self, request: ModelRequest) -> ModelResponse: ...


@runtime_checkable
class TokenCounter(Protocol):
    """Optional provider capability: count a request's input tokens exactly, as the provider's
    tokenizer would. Implemented by adapters whose API offers it (e.g. Anthropic count_tokens)."""

    async def count_input_tokens(self, request: ModelRequest) -> int: ...


@runtime_checkable
class ModelResolver(Protocol):
    """Resolves a logical model id to the provider for its deployment (the gateway, ADR-0011)."""

    def resolve(self, model: str) -> ModelProvider: ...


class UnknownModel(LookupError):
    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"no deployment named {model!r}")


class SingleProvider:
    """A resolver for exactly one provider, addressed by its deployment id."""

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider

    def resolve(self, model: str) -> ModelProvider:
        if model != self._provider.descriptor.deployment:
            raise UnknownModel(model)
        return self._provider
