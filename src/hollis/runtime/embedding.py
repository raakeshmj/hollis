"""The embedding port: a model task distinct from text generation (ADR-0022).

An embedding deployment turns text into vectors in one *embedding space*. Vectors are comparable
only within the same space, so every stored vector is tagged with its space, and retrieval never
compares vectors across spaces. Changing the model, its dimensions or the operator-declared
version is a new space and requires re-embedding.

Providers raise `ModelCallError` (ADR-0007) like text-generation providers. Embedding requests
have no side effects, so retrying them is always safe.
"""

import hashlib
import math
from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from hollis.runtime.model import DeploymentDescriptor


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InputType(StrEnum):
    """Some models embed queries and documents differently (e.g. with instruction prefixes)."""

    QUERY = "query"
    DOCUMENT = "document"


class EmbeddingRequest(_Frozen):
    inputs: tuple[str, ...] = Field(min_length=1)
    input_type: InputType
    timeout_s: float = Field(default=60.0, gt=0)


class EmbeddingResponse(_Frozen):
    vectors: tuple[tuple[float, ...], ...]
    input_tokens: int | None = None
    """As reported by the provider; None when not reported."""
    served_model: str | None = None


class EmbeddingSpace(_Frozen):
    """The identity of a vector space. Two vectors are comparable only if their spaces match."""

    deployment: str
    model: str
    dimensions: int = Field(ge=1)
    version: str = "1"
    """Operator-declared; bump it when the served model changes behind the same name."""

    @property
    def key(self) -> str:
        raw = f"{self.deployment}\x00{self.model}\x00{self.dimensions}\x00{self.version}"
        return "emb_" + hashlib.sha256(raw.encode()).hexdigest()[:20]


class EmbeddingProvider(Protocol):
    @property
    def descriptor(self) -> DeploymentDescriptor: ...

    @property
    def space(self) -> EmbeddingSpace: ...

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse: ...


class EmbeddingMismatch(ValueError):
    """Vectors from different embedding spaces (or of the wrong size) were about to be mixed."""


async def embed_all(
    provider: EmbeddingProvider, texts: Sequence[str], input_type: InputType
) -> list[tuple[float, ...]]:
    """Embed any number of texts in batches the deployment accepts, truncating over-long inputs,
    and return unit-length vectors checked against the space's dimensions."""
    caps = provider.descriptor.capabilities.embedding
    if caps is None:
        raise EmbeddingMismatch(
            f"{provider.descriptor.deployment!r} is not an embedding deployment"
        )
    vectors: list[tuple[float, ...]] = []
    for start in range(0, len(texts), caps.max_batch):
        batch = tuple(
            t[: caps.max_input_chars] or " " for t in texts[start : start + caps.max_batch]
        )
        response = await provider.embed(EmbeddingRequest(inputs=batch, input_type=input_type))
        if len(response.vectors) != len(batch):
            raise EmbeddingMismatch(f"asked for {len(batch)} vectors, got {len(response.vectors)}")
        for vector in response.vectors:
            if len(vector) != provider.space.dimensions:
                raise EmbeddingMismatch(
                    f"vector has {len(vector)} dimensions; "
                    f"the space declares {provider.space.dimensions}"
                )
            vectors.append(normalize(vector))
    return vectors


def normalize(vector: Sequence[float]) -> tuple[float, ...]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0 or not math.isfinite(norm):
        return tuple(0.0 for _ in vector)
    return tuple(x / norm for x in vector)
