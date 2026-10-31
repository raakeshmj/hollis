"""Execution budgets and retry policy for model calls (ADR-0007)."""

from collections.abc import Callable
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

INITIAL_BACKOFF_S = 0.5
MAX_BACKOFF_S = 8.0
MAX_RETRY_WAIT_S = 600.0
"""Longer provider-requested waits fail the run instead of sleeping."""


class AmbiguousOutcomePolicy(StrEnum):
    FAIL = "fail"
    """Fail the run rather than risk a second generation (and charge)."""
    RETRY = "retry"
    """Retry under the same operation id. Only duplicate-free if the provider deduplicates."""


class RunBudget(BaseModel):
    """Configured limits for one run. Persisted in RunCreated so recovery enforces the same."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_output_tokens: int = Field(ge=1)
    """Upper bound on generated tokens per model call, enforced by the provider."""
    max_attempts: int = Field(default=3, ge=1, le=20)
    """Physical attempts per logical model call, including the first."""
    attempt_timeout_s: float = Field(default=120.0, gt=0, le=3600)
    deadline_s: float | None = Field(default=None, gt=0)
    """Wall-clock limit measured from run creation; survives crashes and resumes."""
    max_model_turns: int = Field(default=8, ge=1, le=1000)
    """Logical model calls per run (each tool round trip adds one)."""
    max_tool_calls: int = Field(default=32, ge=0, le=1000)
    """Tool invocations per run that may be authorized; further calls are rejected."""
    max_tool_calls_per_turn: int = Field(default=8, ge=0, le=100)
    max_parallel_tools: int = Field(default=4, ge=1, le=32)
    """Concurrent executions; only batches of read-only tools run concurrently."""
    max_tool_output_chars: int = Field(default=16_000, ge=256, le=1_000_000)
    """Per result; longer output is truncated with an explicit marker."""
    approval_timeout_s: float = Field(default=3600.0, gt=0)
    """Pending approvals expire after this long and the call is denied."""


def default_ambiguous_policy(*, provider_deduplicates: bool) -> AmbiguousOutcomePolicy:
    return AmbiguousOutcomePolicy.RETRY if provider_deduplicates else AmbiguousOutcomePolicy.FAIL


def retry_delay(attempt: int, retry_after_s: float | None, jitter: Callable[[], float]) -> float:
    """Seconds to wait before attempt `attempt + 1`.

    A provider-supplied retry-after is honoured exactly; otherwise capped exponential backoff
    with "equal jitter" (between half and all of the step). `jitter` returns values in [0, 1].
    """
    if retry_after_s is not None:
        return max(retry_after_s, 0.0)
    step = min(INITIAL_BACKOFF_S * 2 ** (attempt - 1), MAX_BACKOFF_S)
    return step * (0.5 + 0.5 * jitter())
