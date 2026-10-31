"""The embedding task: OpenAI-compatible adapter, local hashing embedder, batching, gateway."""

import math
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from hollis.adapters.config import BearerAuth, EndpointConfig, NoAuth, OpenAIEmbeddingsOptions
from hollis.adapters.local_embeddings import HashingEmbedder
from hollis.adapters.openai_embeddings import OpenAIEmbeddingsProvider
from hollis.gateway import Catalog, ModelGateway
from hollis.gateway.gateway import RoutingDenied, WrongTask
from hollis.runtime.embedding import (
    EmbeddingMismatch,
    EmbeddingRequest,
    InputType,
    embed_all,
)
from hollis.runtime.model import (
    DeploymentDescriptor,
    EmbeddingCapabilities,
    FailureKind,
    Locality,
    ModelCallError,
    ModelCapabilities,
    Task,
)
from tests.support.mock_http import MockHTTP

DESCRIPTOR = DeploymentDescriptor(
    deployment="embed",
    provider="ollama",
    protocol="openai-embeddings",
    model="nomic-embed-text",
    endpoint="http://127.0.0.1:11434",
    locality=Locality.LOCAL,
    deduplicates_requests=False,
    capabilities=ModelCapabilities(
        tasks=frozenset({Task.EMBEDDING}),
        text=None,
        embedding=EmbeddingCapabilities(dimensions=3, max_batch=2, max_input_chars=10),
    ),
)


def _reply(*vectors: list[float], shuffle: bool = False, tokens: int | None = 7) -> httpx2.Response:
    data = [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)]
    if shuffle:
        data.reverse()
    body: dict[str, Any] = {"object": "list", "data": data, "model": "nomic-embed-text"}
    if tokens is not None:
        body["usage"] = {"prompt_tokens": tokens, "total_tokens": tokens}
    return httpx2.Response(200, json=body)


def _provider(
    mock: MockHTTP, options: OpenAIEmbeddingsOptions | None = None, **endpoint: Any
) -> OpenAIEmbeddingsProvider:
    config = EndpointConfig(
        base_url=endpoint.pop("base_url", "http://127.0.0.1:11434/v1"),
        auth=endpoint.pop("auth", NoAuth()),
    )
    return OpenAIEmbeddingsProvider(
        DESCRIPTOR,
        config,
        endpoint.pop("credential", None),
        options,
        http_client=mock.http_client(),
    )


async def test_request_shape_prefixes_and_order() -> None:
    mock = MockHTTP(_reply([1, 0, 0], [0, 1, 0], shuffle=True))
    options = OpenAIEmbeddingsOptions(
        query_prefix="search_query: ", document_prefix="search_document: "
    )
    provider = _provider(mock, options)
    response = await provider.embed(EmbeddingRequest(inputs=("a", "b"), input_type=InputType.QUERY))

    assert mock.requests[0].url.path == "/v1/embeddings"
    assert mock.body() == {
        "model": "nomic-embed-text",
        "input": ["search_query: a", "search_query: b"],
        "encoding_format": "float",
    }
    assert response.vectors == ((1, 0, 0), (0, 1, 0))  # re-ordered by index
    assert response.input_tokens == 7
    assert provider.space.dimensions == 3


async def test_credentials_go_only_in_the_header() -> None:
    mock = MockHTTP(_reply([1, 0, 0]))
    provider = _provider(
        mock, auth=BearerAuth(env="EMBED_KEY"), credential=SecretStr("key-0123456789")
    )
    await provider.embed(EmbeddingRequest(inputs=("x",), input_type=InputType.DOCUMENT))
    assert mock.requests[0].headers["authorization"] == "Bearer key-0123456789"
    assert "key-0123456789" not in mock.requests[0].content.decode()


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (_reply([1, 0]), "2 dimensions"),
        (_reply([1, 0, 0], [0, 1, 0]), "expected 1 embeddings"),
        (httpx2.Response(200, json={"unexpected": True}), "not an embeddings response"),
    ],
    ids=["wrong-dimensions", "wrong-count", "wrong-shape"],
)
async def test_invalid_responses_are_rejected(reply: httpx2.Response, message: str) -> None:
    provider = _provider(MockHTTP(reply))
    with pytest.raises(ModelCallError, match=message) as caught:
        await provider.embed(EmbeddingRequest(inputs=("x",), input_type=InputType.DOCUMENT))
    assert caught.value.kind is FailureKind.INVALID_RESPONSE


async def test_status_and_transport_failures_are_classified() -> None:
    limited = _provider(
        MockHTTP(
            httpx2.Response(429, json={"error": {"message": "slow"}}, headers={"retry-after": "2"})
        )
    )
    with pytest.raises(ModelCallError) as caught:
        await limited.embed(EmbeddingRequest(inputs=("x",), input_type=InputType.QUERY))
    assert caught.value.kind is FailureKind.RATE_LIMITED
    down = _provider(MockHTTP(httpx2.ConnectError("refused")))
    with pytest.raises(ModelCallError) as caught:
        await down.embed(EmbeddingRequest(inputs=("x",), input_type=InputType.QUERY))
    assert caught.value.kind is FailureKind.CONNECTION_FAILED


async def test_embed_all_batches_truncates_and_normalizes() -> None:
    mock = MockHTTP(_reply([3, 4, 0], [0, 0, 2]), _reply([1, 1, 1]))
    vectors = await embed_all(_provider(mock), ["a" * 50, "b", "c"], InputType.DOCUMENT)
    assert mock.calls == 2  # max_batch=2
    assert mock.body(0)["input"][0] == "a" * 10  # max_input_chars=10
    assert vectors[0] == pytest.approx((0.6, 0.8, 0.0))
    assert all(math.isclose(sum(x * x for x in v), 1.0) for v in vectors)


async def test_embed_all_refuses_vectors_of_the_wrong_space() -> None:
    class Liar(HashingEmbedder):
        async def embed(self, request: EmbeddingRequest) -> Any:
            response = await super().embed(request)
            return response.model_copy(update={"vectors": tuple(v[:-1] for v in response.vectors)})

    with pytest.raises(EmbeddingMismatch, match="dimensions"):
        await embed_all(Liar(dimensions=8), ["x"], InputType.QUERY)


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_hashing_embedder_captures_surface_similarity_not_meaning() -> None:
    embedder = HashingEmbedder(dimensions=512)
    identifier = embedder.vector("CUDA_ERROR_OUT_OF_MEMORY on worker-7")
    prose = embedder.vector("the job ran CUDA out of memory")
    unrelated = embedder.vector("quarterly revenue grew in the retail segment")
    synonym = embedder.vector("the GPU exhausted its RAM")
    assert embedder.vector("CUDA out of memory") == embedder.vector("CUDA out of memory")
    assert _cosine(identifier, prose) > 0.3
    assert _cosine(identifier, prose) > 3 * abs(_cosine(identifier, unrelated))
    assert _cosine(prose, synonym) < 0.15  # no meaning: a synonym phrasing is not close
    assert embedder.space.dimensions == 512
    assert embedder.descriptor.capabilities.embedding is not None


CATALOG = {
    "deployments": [
        {
            "id": "embed-local",
            "protocol": "openai-embeddings",
            "provider": "ollama",
            "model": "nomic-embed-text",
            "locality": "local",
            "endpoint": {"base_url": "http://127.0.0.1:11434/v1"},
            "capabilities": {"embedding": {"dimensions": 3}},
            "version": "2026-11",
        },
        {
            "id": "embed-cloud",
            "protocol": "openai-embeddings",
            "provider": "openai",
            "model": "text-embedding-3-small",
            "locality": "external",
            "endpoint": {
                "base_url": "https://api.openai.com/v1",
                "auth": {"type": "bearer", "env": "OPENAI_API_KEY"},
            },
            "capabilities": {"embedding": {"dimensions": 3}},
        },
        {
            "id": "chat",
            "protocol": "openai-chat-completions",
            "provider": "ollama",
            "model": "qwen3",
            "locality": "local",
            "endpoint": {"base_url": "http://127.0.0.1:11434/v1"},
        },
    ]
}


async def test_gateway_resolves_embedders_separately_from_text_models() -> None:
    gateway = ModelGateway(
        Catalog.from_mapping(CATALOG),
        environ={"OPENAI_API_KEY": "sk-test-0123456789abcdef"},
        http_client_factory=lambda endpoint: MockHTTP(_reply([1, 0, 0])).http_client(),
    )
    embedder = gateway.embedder("embed-local")
    assert embedder.space.version == "2026-11"
    assert embedder.descriptor.capabilities.tasks == frozenset({Task.EMBEDDING})
    response = await embedder.embed(EmbeddingRequest(inputs=("x",), input_type=InputType.QUERY))
    assert response.vectors == ((1, 0, 0),)
    with pytest.raises(WrongTask):
        gateway.resolve("embed-local")
    with pytest.raises(WrongTask):
        gateway.embedder("chat")
    gateway.embedder("embed-cloud")
    assert gateway.redactor.contains_secret("sk-test-0123456789abcdef")
    await gateway.aclose()


def test_routing_policy_applies_to_embedders() -> None:
    data = {**CATALOG, "routing": {"allowed_localities": ["local", "private"]}}
    gateway = ModelGateway(Catalog.from_mapping(data), environ={})
    with pytest.raises(RoutingDenied):
        gateway.embedder("embed-cloud")


def test_embedding_deployments_must_declare_dimensions() -> None:
    bad = {"deployments": [{**CATALOG["deployments"][0], "capabilities": {}}]}
    with pytest.raises(ValueError, match="dimensions"):
        Catalog.from_mapping(bad)
