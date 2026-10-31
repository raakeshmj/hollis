"""OpenAI-compatible `/v1/embeddings` adapter (ADR-0022).

One wire protocol served by OpenAI and by self-hosted servers (Ollama, vLLM, llama.cpp, LM Studio,
Text Embeddings Inference, Infinity). Raw httpx2 over the shared HTTP layer (ADR-0012): explicit
endpoint, auth by reference, TLS and proxy configuration, no environment-driven routing.

Every response is validated: one vector per input, in input order, with the declared number of
dimensions. A mismatch is an invalid response, never silently accepted.
"""

from types import TracebackType
from typing import Any, Self

import httpx2
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from hollis.adapters._http import build_async_client, error_type, status_error, transport_failure
from hollis.adapters.config import (
    BearerAuth,
    EndpointConfig,
    HeaderAuth,
    NoAuth,
    OpenAIEmbeddingsOptions,
)
from hollis.runtime.embedding import EmbeddingRequest, EmbeddingResponse, EmbeddingSpace, InputType
from hollis.runtime.model import DeploymentDescriptor, FailureKind, ModelCallError

PROTOCOL = "openai-embeddings"


class _Tolerant(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Item(_Tolerant):
    embedding: list[float]
    index: int = Field(ge=0)


class _Usage(_Tolerant):
    prompt_tokens: int | None = None
    total_tokens: int | None = None


class _Response(_Tolerant):
    data: list[_Item]
    model: str | None = None
    usage: _Usage | None = None


class OpenAIEmbeddingsProvider:
    def __init__(
        self,
        descriptor: DeploymentDescriptor,
        endpoint: EndpointConfig,
        credential: SecretStr | None,
        options: OpenAIEmbeddingsOptions | None = None,
        *,
        version: str = "1",
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        caps = descriptor.capabilities.embedding
        if caps is None:
            raise ValueError(
                f"deployment {descriptor.deployment!r} declares no embedding capabilities"
            )
        self._descriptor = descriptor
        self._space = EmbeddingSpace(
            deployment=descriptor.deployment,
            model=descriptor.model,
            dimensions=caps.dimensions,
            version=version,
        )
        self._url = f"{endpoint.base_url}/embeddings"
        self._options = options or OpenAIEmbeddingsOptions()
        self._client = http_client or build_async_client(endpoint)
        self._headers = {"accept": "application/json", **endpoint.headers}
        match endpoint.auth:
            case BearerAuth():
                self._headers["authorization"] = f"Bearer {_require(credential)}"
            case HeaderAuth(header=header):
                self._headers[header] = _require(credential)
            case NoAuth():
                pass

    @property
    def descriptor(self) -> DeploymentDescriptor:
        return self._descriptor

    @property
    def space(self) -> EmbeddingSpace:
        return self._space

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def request_body(self, request: EmbeddingRequest) -> dict[str, Any]:
        options = self._options
        prefix = (
            options.query_prefix
            if request.input_type is InputType.QUERY
            else options.document_prefix
        )
        body: dict[str, Any] = {
            "model": self._descriptor.model,
            "input": [prefix + text for text in request.inputs],
            "encoding_format": "float",
        }
        if options.send_dimensions:
            body["dimensions"] = self._space.dimensions
        body.update(options.extra_body)
        return body

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        try:
            response = await self._client.post(
                self._url,
                json=self.request_body(request),
                headers=self._headers,
                timeout=request.timeout_s,
            )
        except httpx2.TransportError as exc:
            raise transport_failure(exc) from exc
        request_id = response.headers.get("x-request-id")
        try:
            body: object = response.json()
        except ValueError:
            body = None
        if response.status_code >= 400:
            raise status_error(
                response.status_code,
                body,
                response.headers,
                error_type=error_type(body),
                request_id=request_id,
            )
        try:
            parsed = _Response.model_validate(body)
        except ValidationError as exc:
            raise self._invalid(
                f"not an embeddings response ({exc.error_count()} validation errors)", request_id
            ) from exc
        items = sorted(parsed.data, key=lambda item: item.index)
        if [item.index for item in items] != list(range(len(request.inputs))):
            raise self._invalid(
                f"expected {len(request.inputs)} embeddings in order, got {len(items)}", request_id
            )
        dimensions = self._space.dimensions
        for item in items:
            if len(item.embedding) != dimensions:
                raise self._invalid(
                    f"embedding has {len(item.embedding)} dimensions; "
                    f"the deployment declares {dimensions}",
                    request_id,
                )
        usage = parsed.usage
        tokens = (
            None
            if usage is None
            else (usage.prompt_tokens if usage.prompt_tokens is not None else usage.total_tokens)
        )
        return EmbeddingResponse(
            vectors=tuple(tuple(item.embedding) for item in items),
            input_tokens=tokens,
            served_model=parsed.model,
        )

    @staticmethod
    def _invalid(message: str, request_id: str | None) -> ModelCallError:
        return ModelCallError(FailureKind.INVALID_RESPONSE, message, provider_request_id=request_id)


def _require(credential: SecretStr | None) -> str:
    if credential is None:
        raise ValueError("this endpoint's auth requires a credential")
    return credential.get_secret_value()
