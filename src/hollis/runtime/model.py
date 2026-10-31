"""Provider-neutral model types and the ModelProvider port, owned by the runtime core."""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Usage(_Frozen):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class ModelResponse(_Frozen):
    content: str
    usage: Usage | None = None


class ModelRequest(_Frozen):
    operation_id: str = Field(min_length=1)
    """Idempotency key: identical for every attempt of one logical model call."""
    model: str = Field(min_length=1)
    input: str
    instructions: str | None = None


class ModelProviderError(Exception):
    """The provider definitively rejected the request without producing a response.

    Raising this records a terminal failure. Any other exception is treated as an ambiguous
    outcome: the run stays resumable and the call is retried under the same operation id.
    """


class ModelProvider(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse: ...
