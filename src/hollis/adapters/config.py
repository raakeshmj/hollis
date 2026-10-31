"""Endpoint and protocol-option configuration for model adapters (ADR-0011, ADR-0013).

Pure configuration: no SDK or HTTP imports, so the gateway can load and validate a catalog
without importing any provider dependency. Credentials appear only as references (environment
variable names); their values are resolved by the gateway at the credential boundary.
"""

import ipaddress
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"
_SECRET_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "x-api-key", "api-key", "cookie"}
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NoAuth(_Frozen):
    """No credential is sent (typical for localhost inference servers)."""

    type: Literal["none"] = "none"


class BearerAuth(_Frozen):
    """`Authorization: Bearer <secret>`, with the secret read from an environment variable."""

    type: Literal["bearer"] = "bearer"
    env: str = Field(pattern=_ENV_NAME)


class HeaderAuth(_Frozen):
    """The secret sent verbatim in a named header (e.g. `x-api-key`, `api-key`)."""

    type: Literal["header"] = "header"
    header: str = Field(min_length=1)
    env: str = Field(pattern=_ENV_NAME)


AuthConfig = Annotated[NoAuth | BearerAuth | HeaderAuth, Field(discriminator="type")]


class TlsConfig(_Frozen):
    verify: bool = True
    """Disabling verification is an explicit, logged opt-in for test environments only."""
    ca_bundle: Path | None = None
    """PEM file of trusted CA certificates, e.g. an enterprise or on-premises CA."""
    client_cert: Path | None = None
    client_key: Path | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.client_key is not None and self.client_cert is None:
            raise ValueError("client_key requires client_cert")
        if not self.verify and self.ca_bundle is not None:
            raise ValueError("ca_bundle has no effect with verify disabled")
        return self


class EndpointConfig(_Frozen):
    base_url: str
    """The API root, e.g. https://api.anthropic.com or http://127.0.0.1:8000/v1."""
    auth: AuthConfig = NoAuth()
    tls: TlsConfig = TlsConfig()
    proxy: str | None = None
    """Explicit proxy URL. Proxy environment variables are never consulted."""
    headers: dict[str, str] = Field(default_factory=dict[str, str])
    """Extra non-secret headers. Credential headers must use `auth` instead."""

    @field_validator("base_url", "proxy")
    @classmethod
    def _safe_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"{value!r} must be an absolute http(s) URL")
        if parts.username or parts.password:
            raise ValueError("URLs must not embed credentials; use `auth` with an env reference")
        if parts.query or parts.fragment:
            raise ValueError("URLs must not contain a query string or fragment")
        return value.rstrip("/")

    @field_validator("headers")
    @classmethod
    def _no_secret_headers(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            if name.lower() in _SECRET_HEADERS:
                raise ValueError(f"header {name!r} carries credentials; configure it via `auth`")
        return value

    @property
    def origin(self) -> str:
        """scheme://host[:port], safe to persist and display."""
        parts = urlsplit(self.base_url)
        host = f"[{parts.hostname}]" if parts.hostname and ":" in parts.hostname else parts.hostname
        return f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")

    @property
    def is_loopback(self) -> bool:
        host = urlsplit(self.base_url).hostname or ""
        if host == "localhost" or host.endswith(".localhost"):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class AnthropicMessagesOptions(_Frozen):
    effort: Effort | None = None
    """Sent as `output_config.effort` only when set and declared in the capabilities."""
    anthropic_version: str | None = None
    """Override the `anthropic-version` header (compatible servers may expect an older one)."""
    quota_on_429_without_retry_after: bool = False
    """Anthropic's API documents that a spend-cap 429 has no retry-after. Enable only for
    endpoints that follow that convention; compatible servers generally don't."""


_MANAGED_CHAT_FIELDS = frozenset(
    {
        "model",
        "messages",
        "max_tokens",
        "max_completion_tokens",
        "reasoning_effort",
        "stream",
        "stream_options",
        "n",
    }
)


class OpenAIChatOptions(_Frozen):
    token_limit_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"  # noqa: S105  # a field name, not a secret
    """OpenAI deprecated `max_tokens`; many compatible servers only accept `max_tokens`."""
    system_role: Literal["system", "developer"] = "system"
    reasoning_effort: str | None = None
    """Sent only when set and declared in the capabilities."""
    extra_body: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    """Server-specific request fields (e.g. vLLM `top_k`). Must not override managed fields."""

    @field_validator("extra_body")
    @classmethod
    def _no_managed_fields(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        clashes = sorted(_MANAGED_CHAT_FIELDS & value.keys())
        if clashes:
            raise ValueError(f"extra_body must not set Hollis-managed fields {clashes}")
        return value


_MANAGED_EMBEDDING_FIELDS = frozenset({"model", "input", "encoding_format", "dimensions"})


class OpenAIEmbeddingsOptions(_Frozen):
    query_prefix: str = ""
    """Prepended to queries; some models expect an instruction (e.g. `search_query: `)."""
    document_prefix: str = ""
    """Prepended to documents (e.g. `search_document: `)."""
    send_dimensions: bool = False
    """Send `dimensions` (only for models that support shortened embeddings)."""
    extra_body: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])

    @field_validator("extra_body")
    @classmethod
    def _no_managed_fields(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        clashes = sorted(_MANAGED_EMBEDDING_FIELDS & value.keys())
        if clashes:
            raise ValueError(f"extra_body must not set Hollis-managed fields {clashes}")
        return value
