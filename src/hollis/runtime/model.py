"""Provider-neutral model types owned by the runtime core."""

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Usage(_Frozen):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class ModelResponse(_Frozen):
    content: str
    usage: Usage | None = None
