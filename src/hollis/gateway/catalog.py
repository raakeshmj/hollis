"""Deployment catalog and routing policy (ADR-0011, ADR-0013).

A deployment binds a logical model id to a protocol, an endpoint, a wire model id, a locality
and declared capabilities. Loading a catalog validates everything that can be checked without
network access; it never resolves credentials.
"""

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hollis.adapters.config import AnthropicMessagesOptions, EndpointConfig, OpenAIChatOptions
from hollis.gateway import presets
from hollis.runtime.model import DeploymentDescriptor, Locality, ModelCapabilities

# Hosts that are third-party services by definition; they can never be "local" or "private".
KNOWN_EXTERNAL_HOSTS = frozenset({"api.anthropic.com", "api.openai.com", "ollama.com"})


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class _Deployment(_Frozen):
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
    """Logical model id that runs reference."""
    provider: str = Field(min_length=1)
    """Informational label (anthropic, openai, vllm, ollama, …); also selects presets."""
    model: str = Field(min_length=1)
    """Model identifier sent on the wire."""
    locality: Locality
    endpoint: EndpointConfig
    capabilities: ModelCapabilities = ModelCapabilities()
    deduplicates_requests: bool = False
    """Set True only for an endpoint that documents request deduplication."""

    @model_validator(mode="before")
    @classmethod
    def _apply_presets(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        fields = dict(cast(dict[str, Any], data))
        protocol_field = cls.model_fields.get("protocol")
        if "protocol" not in fields and protocol_field is not None:
            fields["protocol"] = protocol_field.default
        return presets.apply(fields)

    @model_validator(mode="after")
    def _locality_is_plausible(self) -> Self:
        host = self.endpoint.origin.split("://", 1)[1].split(":")[0]
        if self.locality is Locality.LOCAL and not self.endpoint.is_loopback:
            raise ValueError(f"locality 'local' requires a loopback endpoint, not {host!r}")
        if self.locality is not Locality.EXTERNAL and host in KNOWN_EXTERNAL_HOSTS:
            raise ValueError(f"{host!r} is an external service; declare locality 'external'")
        return self

    def _require_effort(self, value: str | None) -> None:
        if value is None:
            return
        text = self.capabilities.text
        allowed = text.reasoning_effort if text is not None else ()
        if value not in allowed:
            raise ValueError(
                f"effort {value!r} is not declared in capabilities.text.reasoning_effort {allowed}"
            )

    def _descriptor(self, protocol: str) -> DeploymentDescriptor:
        return DeploymentDescriptor(
            deployment=self.id,
            provider=self.provider,
            protocol=protocol,
            model=self.model,
            endpoint=self.endpoint.origin,
            locality=self.locality,
            deduplicates_requests=self.deduplicates_requests,
            capabilities=self.capabilities,
        )


class AnthropicMessagesDeployment(_Deployment):
    protocol: Literal["anthropic-messages"] = "anthropic-messages"
    options: AnthropicMessagesOptions = AnthropicMessagesOptions()

    @model_validator(mode="after")
    def _effort_declared(self) -> Self:
        self._require_effort(self.options.effort)
        return self

    def descriptor(self) -> DeploymentDescriptor:
        return self._descriptor(self.protocol)


class OpenAIChatDeployment(_Deployment):
    protocol: Literal["openai-chat-completions"] = "openai-chat-completions"
    options: OpenAIChatOptions = OpenAIChatOptions()

    @model_validator(mode="after")
    def _effort_declared(self) -> Self:
        self._require_effort(self.options.reasoning_effort)
        return self

    def descriptor(self) -> DeploymentDescriptor:
        return self._descriptor(self.protocol)


Deployment = Annotated[
    AnthropicMessagesDeployment | OpenAIChatDeployment, Field(discriminator="protocol")
]


class RoutingPolicy(_Frozen):
    allowed_localities: frozenset[Locality] = frozenset(Locality)
    """E.g. {local, private} for deployments where data must not leave operator infrastructure."""


class Catalog(_Frozen):
    deployments: tuple[Deployment, ...] = ()
    routing: RoutingPolicy = RoutingPolicy()

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        ids = [d.id for d in self.deployments]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"duplicate deployment ids {duplicates}")
        return self

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Self:
        return cls.model_validate(dict(data))

    @classmethod
    def from_toml(cls, path: str | Path) -> Self:
        with Path(path).open("rb") as handle:
            return cls.from_mapping(tomllib.load(handle))

    def get(self, model: str) -> AnthropicMessagesDeployment | OpenAIChatDeployment | None:
        return next((d for d in self.deployments if d.id == model), None)
