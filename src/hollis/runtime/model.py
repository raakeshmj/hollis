"""Provider-neutral model types and the ModelProvider port, owned by the runtime core.

Providers translate their own failures into `ModelCallError` with a `FailureKind`. The kind
determines the `CallOutcome` (whether the provider may have generated, and charged for, a
response) and whether a retry can be safe. See ADR-0007.
"""

from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


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


class StopReason(StrEnum):
    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    REFUSAL = "refusal"
    CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"
    OTHER = "other"


class ModelResponse(_Frozen):
    content: str
    stop_reason: StopReason = StopReason.END_TURN
    usage: Usage | None = None
    served_model: str | None = None
    """Model identifier the provider reports having used."""
    provider_request_id: str | None = None
    omitted_block_types: tuple[str, ...] = ()
    """Non-text content block types present in the provider response but not mapped."""


class ModelRequest(_Frozen):
    operation_id: str = Field(min_length=1)
    """Idempotency key: identical for every attempt of one logical model call."""
    model: str = Field(min_length=1)
    input: str
    instructions: str | None = None
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


class ProviderCapabilities(_Frozen):
    name: str = Field(min_length=1)
    """Stable provider identifier, recorded in each run so recovery uses the same provider."""
    deduplicates_requests: bool
    """True only if the provider guarantees that repeating a request with the same operation
    id cannot produce a second generation. Determines the default ambiguous-outcome policy."""


class ModelProvider(Protocol):
    @property
    def capabilities(self) -> ProviderCapabilities: ...

    async def generate(self, request: ModelRequest) -> ModelResponse: ...
