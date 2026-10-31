"""Provider-neutral conversation and tool-call types (ADR-0014).

These are what the runtime persists and what adapters translate to and from each provider's wire
format. Provider-specific content that must be echoed back verbatim to continue a conversation
(e.g. Anthropic thinking blocks with signatures) travels as an opaque `ProviderContinuation`
owned by the adapter for that protocol.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

TOOL_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
"""The intersection of what Anthropic and OpenAI accept for tool names."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolSpec(_Frozen):
    """A tool as described to the model. Derived from the same schema used for validation."""

    name: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str
    input_schema: dict[str, JsonValue]


class ToolCall(_Frozen):
    """A model's request to run a tool, normalized across providers. A request, not a grant."""

    call_id: str
    """The provider's identifier, echoed back with the result."""
    name: str
    arguments: dict[str, JsonValue] | None = None
    """Parsed arguments; None when the provider's arguments were not a JSON object."""
    raw_arguments: str | None = None
    """The provider's original argument string, when it sends one (OpenAI-style)."""
    parse_error: str | None = None


class ProviderContinuation(_Frozen):
    """Opaque, protocol-owned content that must be sent back unchanged on the next turn."""

    protocol: str
    data: JsonValue


class ToolResultMessage(_Frozen):
    """A tool result as sent to the model: already rendered, limited, redacted and framed."""

    call_id: str
    name: str
    content: str
    is_error: bool = False


class AssistantTurn(_Frozen):
    type: Literal["assistant"] = "assistant"
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    continuation: ProviderContinuation | None = None


class ToolResultsTurn(_Frozen):
    type: Literal["tool_results"] = "tool_results"
    results: tuple[ToolResultMessage, ...]


HistoryItem = Annotated[AssistantTurn | ToolResultsTurn, Field(discriminator="type")]
