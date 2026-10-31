"""The model gateway: resolves logical model ids to providers (ADR-0011).

Responsibilities: deployment lookup, routing policy, credential resolution (registered with the
shared Redactor before any request is sent), and lazy, cached adapter construction. It does no
model routing, fallback or load balancing: a model id resolves to exactly one deployment, or
resolution fails.

Adapter modules are imported only when a deployment of their protocol is first resolved, so a
catalog of local OpenAI-compatible deployments never imports the Anthropic SDK.
"""

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import SecretStr

from hollis.adapters.config import BearerAuth, EndpointConfig, HeaderAuth
from hollis.adapters.credentials import require_env_secret
from hollis.gateway.catalog import (
    AnthropicMessagesDeployment,
    AnyDeployment,
    Catalog,
    OpenAIChatDeployment,
    OpenAIEmbeddingsDeployment,
)
from hollis.runtime.embedding import EmbeddingProvider
from hollis.runtime.model import ModelProvider, Task, UnknownModel
from hollis.runtime.redaction import Redactor

if TYPE_CHECKING:
    import httpx2

    from hollis.runtime.runner import Runner
    from hollis.runtime.store import RunStore


class RoutingDenied(PermissionError):
    def __init__(self, model: str, locality: str) -> None:
        super().__init__(f"routing policy forbids {locality} deployment {model!r}")


class _Closable(ModelProvider, Protocol):
    async def aclose(self) -> None: ...


class _ClosableEmbedder(EmbeddingProvider, Protocol):
    async def aclose(self) -> None: ...


class WrongTask(LookupError):
    """A deployment was resolved for a task it does not serve."""


type ClientFactory = Callable[[EndpointConfig], "httpx2.AsyncClient"]


class ModelGateway:
    def __init__(
        self,
        catalog: Catalog,
        *,
        environ: Mapping[str, str] | None = None,
        redactor: Redactor | None = None,
        http_client_factory: ClientFactory | None = None,
    ) -> None:
        """`environ` defaults to the process environment and is read only for credentials named
        by deployment auth. `http_client_factory` lets tests inject transports."""
        self._catalog = catalog
        self._environ = environ
        self._redactor = redactor or Redactor()
        self._client_factory = http_client_factory
        self._providers: dict[str, _Closable] = {}
        self._embedders: dict[str, _ClosableEmbedder] = {}

    @property
    def catalog(self) -> Catalog:
        return self._catalog

    @property
    def redactor(self) -> Redactor:
        """Shared with the Runner so every resolved credential is redacted from run data."""
        return self._redactor

    def resolve(self, model: str) -> ModelProvider:
        """The text-generation provider for `model`."""
        deployment = self._allowed(model)
        if isinstance(deployment, OpenAIEmbeddingsDeployment):
            raise WrongTask(f"{model!r} is an embedding deployment; use embedder()")
        provider = self._providers.get(model)
        if provider is None:
            provider = self._providers[model] = self._build(deployment)
        return provider

    def embedder(self, model: str) -> EmbeddingProvider:
        """The embedding provider for `model` (same routing policy and credentials rules)."""
        deployment = self._allowed(model)
        if not isinstance(deployment, OpenAIEmbeddingsDeployment):
            raise WrongTask(
                f"{model!r} is not an embedding deployment ({Task.EMBEDDING} not served)"
            )
        embedder = self._embedders.get(model)
        if embedder is None:
            from hollis.adapters.openai_embeddings import OpenAIEmbeddingsProvider

            embedder = self._embedders[model] = OpenAIEmbeddingsProvider(
                deployment.descriptor(),
                deployment.endpoint,
                self._credential(deployment.endpoint),
                deployment.options,
                version=deployment.version,
                http_client=self._http_client(deployment.endpoint),
            )
        return embedder

    def _allowed(self, model: str) -> AnyDeployment:
        deployment = self._catalog.get(model)
        if deployment is None:
            raise UnknownModel(model)
        if deployment.locality not in self._catalog.routing.allowed_localities:
            raise RoutingDenied(model, deployment.locality.value)
        return deployment

    def runner(self, store: "RunStore", **kwargs: Any) -> "Runner":
        from hollis.runtime.runner import Runner

        return Runner(store, self, redactor=self._redactor, **kwargs)

    async def aclose(self) -> None:
        providers, self._providers = list(self._providers.values()), {}
        embedders, self._embedders = list(self._embedders.values()), {}
        for provider in providers:
            await provider.aclose()
        for embedder in embedders:
            await embedder.aclose()

    def _credential(self, endpoint: EndpointConfig) -> SecretStr | None:
        if not isinstance(endpoint.auth, BearerAuth | HeaderAuth):
            return None
        secret = require_env_secret(endpoint.auth.env, self._environ)
        self._redactor.register(secret)
        return secret

    def _http_client(self, endpoint: EndpointConfig) -> "httpx2.AsyncClient | None":
        return self._client_factory(endpoint) if self._client_factory else None

    def _build(self, deployment: AnthropicMessagesDeployment | OpenAIChatDeployment) -> _Closable:
        credential = self._credential(deployment.endpoint)
        descriptor = deployment.descriptor()
        match deployment:
            case AnthropicMessagesDeployment():
                from hollis.adapters.anthropic_messages import AnthropicMessagesProvider

                return AnthropicMessagesProvider.create(
                    descriptor,
                    deployment.endpoint,
                    credential,
                    deployment.options,
                    http_client=self._http_client(deployment.endpoint),
                )
            case OpenAIChatDeployment():
                from hollis.adapters.openai_chat import OpenAIChatProvider

                return OpenAIChatProvider(
                    descriptor,
                    deployment.endpoint,
                    credential,
                    deployment.options,
                    http_client=self._http_client(deployment.endpoint),
                )
