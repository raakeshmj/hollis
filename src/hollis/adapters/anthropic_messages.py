"""Anthropic Messages protocol adapter, using the official `anthropic` SDK (ADR-0008, ADR-0012).

Serves Anthropic's API and any endpoint that implements the Messages protocol well enough for
the declared capabilities (e.g. vLLM, llama.cpp, Ollama, LM Studio `/v1/messages`). Requires the
optional extra: `pip install hollis[anthropic]`.

- Everything about the endpoint is explicit: base URL, auth, TLS, proxy and version. The SDK's
  environment fallbacks (`ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
  `ANTHROPIC_CUSTOM_HEADERS`, profiles) are never consulted, so environment variables cannot
  redirect run data or inject headers.
- Hollis owns retries: every call uses `max_retries=0`. SDK telemetry is disabled.
- `thinking` is never sent. `output_config.effort` is sent only when configured; the gateway
  checks it against the deployment's declared capabilities.
- Failures are classified from status codes, headers and transport exception types, never from
  message text. SDK types never leave this module.
"""

import os
from types import TracebackType
from typing import Self

import httpx2
from anthropic import APIConnectionError, APIStatusError, AsyncAnthropic, Omit, omit
from anthropic.types import Message
from pydantic import SecretStr

from hollis.adapters._http import build_async_client, parse_retry_after, status_error
from hollis.adapters._http import transport_failure as _transport_failure
from hollis.adapters.config import (
    AnthropicMessagesOptions,
    BearerAuth,
    EndpointConfig,
    HeaderAuth,
    NoAuth,
)
from hollis.runtime.model import (
    DeploymentDescriptor,
    FailureKind,
    ModelCallError,
    ModelRequest,
    ModelResponse,
    StopReason,
    Usage,
)

PROTOCOL = "anthropic-messages"

MAX_NONSTREAMING_OUTPUT_TOKENS = 21_333
"""The SDK's non-streaming bound (requests expected to exceed 10 minutes at 128k tokens per hour
must stream). The SDK only checks it when no timeout is passed; Hollis always passes one."""

# The SDK requires some credential to construct a client. For keyless or header-auth endpoints a
# placeholder is passed and the Authorization header it would produce is omitted per request.
_PLACEHOLDER_KEY = "hollis-no-credential"

_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": StopReason.END_TURN,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "refusal": StopReason.REFUSAL,
    "model_context_window_exceeded": StopReason.CONTEXT_WINDOW_EXCEEDED,
}
_IGNORED_BLOCKS = frozenset({"text", "thinking", "redacted_thinking"})


class AnthropicMessagesProvider:
    def __init__(
        self,
        descriptor: DeploymentDescriptor,
        client: AsyncAnthropic,
        options: AnthropicMessagesOptions | None = None,
        *,
        request_headers: dict[str, str | Omit] | None = None,
    ) -> None:
        """Prefer `create`. A caller-built client (e.g. for cloud-platform auth) is used as-is,
        except that retries are disabled per call."""
        self._descriptor = descriptor
        self._client = client
        self._options = options or AnthropicMessagesOptions()
        self._request_headers = request_headers or {}

    @classmethod
    def create(
        cls,
        descriptor: DeploymentDescriptor,
        endpoint: EndpointConfig,
        credential: SecretStr | None,
        options: AnthropicMessagesOptions | None = None,
        *,
        http_client: httpx2.AsyncClient | None = None,
    ) -> Self:
        options = options or AnthropicMessagesOptions()
        default_headers = dict(endpoint.headers)
        if options.anthropic_version is not None:
            default_headers["anthropic-version"] = options.anthropic_version

        # Neutralize headers the SDK would inject from ANTHROPIC_CUSTOM_HEADERS.
        request_headers: dict[str, str | Omit] = {
            name: omit for name in _env_custom_header_names() if name not in default_headers
        }
        api_key = _PLACEHOLDER_KEY
        match endpoint.auth:
            case BearerAuth():
                api_key = _require(credential).get_secret_value()  # sent as a bearer token
            case HeaderAuth(header=header):
                request_headers["Authorization"] = omit
                request_headers[header] = _require(credential).get_secret_value()
            case NoAuth():
                request_headers["Authorization"] = omit
        client = AsyncAnthropic(
            api_key=api_key,
            base_url=endpoint.base_url,
            max_retries=0,
            open_telemetry=False,
            default_headers=default_headers,
            http_client=http_client or build_async_client(endpoint),
        )
        return cls(descriptor, client, options, request_headers=request_headers)

    @property
    def descriptor(self) -> DeploymentDescriptor:
        return self._descriptor

    async def aclose(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def generate(self, request: ModelRequest) -> ModelResponse:
        if request.max_output_tokens > MAX_NONSTREAMING_OUTPUT_TOKENS:
            raise ModelCallError(
                FailureKind.INVALID_REQUEST,
                f"max_output_tokens {request.max_output_tokens} exceeds the non-streaming limit "
                f"{MAX_NONSTREAMING_OUTPUT_TOKENS}",
            )
        effort = self._options.effort
        client = self._client.with_options(max_retries=0, timeout=request.timeout_s)
        try:
            message = await client.messages.create(
                model=request.model,
                max_tokens=request.max_output_tokens,
                messages=[{"role": "user", "content": request.input}],
                system=request.instructions if request.instructions is not None else omit,
                output_config={"effort": effort} if effort is not None else omit,
                extra_headers=self._request_headers or None,
            )
        except APIStatusError as exc:
            raise self._status_error(exc) from exc
        except APIConnectionError as exc:
            raise _transport_failure(exc.__cause__) from exc
        return _to_response(message)

    def _status_error(self, exc: APIStatusError) -> ModelCallError:
        headers = exc.response.headers
        quota = (
            self._options.quota_on_429_without_retry_after
            and exc.status_code == 429
            and parse_retry_after(headers) is None
        )
        return status_error(
            exc.status_code,
            exc.body,
            headers,
            error_type=exc.type,
            request_id=exc.request_id,
            quota_exhausted=quota,
        )


def _require(credential: SecretStr | None) -> SecretStr:
    if credential is None:
        raise ValueError("this endpoint's auth requires a credential")
    return credential


def _env_custom_header_names() -> list[str]:
    raw = os.environ.get("ANTHROPIC_CUSTOM_HEADERS", "")
    return [line.split(":", 1)[0].strip() for line in raw.splitlines() if ":" in line]


def _to_response(message: Message) -> ModelResponse:
    texts: list[str] = []
    omitted: list[str] = []
    for block in message.content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type not in _IGNORED_BLOCKS:
            omitted.append(block.type)
    usage = message.usage
    details = usage.output_tokens_details
    return ModelResponse(
        content="".join(texts),
        stop_reason=_STOP_REASONS.get(message.stop_reason or "", StopReason.OTHER),
        usage=Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_input_tokens=usage.cache_read_input_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens,
            thinking_tokens=details.thinking_tokens if details is not None else None,
        ),
        served_model=message.model,
        # Public despite the underscore (documented in the SDK and API docs).
        provider_request_id=message._request_id,  # pyright: ignore[reportPrivateUsage]
        provider_response_id=message.id,
        omitted_block_types=tuple(omitted),
    )
