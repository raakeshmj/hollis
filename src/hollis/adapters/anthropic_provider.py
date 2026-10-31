"""Anthropic Claude ModelProvider using the official `anthropic` SDK (ADR-0008).

Requires the optional extra: `pip install hollis[anthropic]`.

- Hollis owns retries: every call is made with `max_retries=0`, so one `generate` call is exactly
  one HTTP request and every physical attempt is visible in the run's event log.
- The Messages API documents no idempotency mechanism and the SDK sends no idempotency key, so
  this provider declares `deduplicates_requests=False`: retrying an ambiguous outcome may produce
  (and bill) a second generation.
- `thinking` is never sent, so each model uses its own default (always-on adaptive thinking on
  current models). `output_config.effort` is sent only when configured, because not every model
  supports it.
- Failures are classified from SDK exception types, HTTP status codes, response headers and the
  underlying transport exception type, never from message text.
- SDK types never leave this module.
"""

from types import TracebackType
from typing import Any, Literal, Self, cast

import httpx2
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    omit,
)
from anthropic.types import Message
from pydantic import BaseModel, ConfigDict, SecretStr

from hollis.adapters.credentials import require_env_secret
from hollis.runtime.model import (
    DeploymentDescriptor,
    FailureKind,
    Locality,
    ModelCallError,
    ModelRequest,
    ModelResponse,
    StopReason,
    Usage,
)

DEFAULT_DESCRIPTOR = DeploymentDescriptor(
    deployment="claude-opus-5-5",
    provider="anthropic",
    protocol="anthropic-messages",
    model="claude-opus-5-5",
    endpoint="https://api.anthropic.com",
    locality=Locality.EXTERNAL,
    deduplicates_requests=False,
)
API_KEY_ENV = "ANTHROPIC_API_KEY"

MAX_NONSTREAMING_OUTPUT_TOKENS = 21_333
"""The SDK's non-streaming bound (requests expected to exceed 10 minutes at 128k tokens per hour
must stream). The SDK only checks it when no timeout is passed; Hollis always passes one."""

MAX_PROVIDER_MESSAGE_CHARS = 300

Effort = Literal["low", "medium", "high", "xhigh", "max"]

_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": StopReason.END_TURN,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "refusal": StopReason.REFUSAL,
    "model_context_window_exceeded": StopReason.CONTEXT_WINDOW_EXCEEDED,
}
_IGNORED_BLOCKS = frozenset({"text", "thinking", "redacted_thinking"})
_NOT_SENT = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)


class AnthropicSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    effort: Effort | None = None
    """Sent as `output_config.effort` when set; otherwise the model's default applies."""


class AnthropicProvider:
    def __init__(self, client: AsyncAnthropic, settings: AnthropicSettings | None = None) -> None:
        """Wrap an existing client (e.g. one using profile or federation auth). Retries are
        disabled per call regardless of the client's own `max_retries`."""
        self._client = client
        self._settings = settings or AnthropicSettings()

    @classmethod
    def from_api_key(
        cls,
        api_key: SecretStr,
        settings: AnthropicSettings | None = None,
        *,
        base_url: str | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> Self:
        client = AsyncAnthropic(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
            max_retries=0,
            open_telemetry=False,  # trace attributes are a Hollis concern, not the SDK's
            http_client=http_client,
        )
        return cls(client, settings)

    @classmethod
    def from_env(cls, settings: AnthropicSettings | None = None) -> Self:
        """Build from `ANTHROPIC_API_KEY`. Raises MissingCredential if it is unset."""
        return cls.from_api_key(require_env_secret(API_KEY_ENV), settings)

    @property
    def descriptor(self) -> DeploymentDescriptor:
        return DEFAULT_DESCRIPTOR

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
        effort = self._settings.effort
        client = self._client.with_options(max_retries=0, timeout=request.timeout_s)
        try:
            message = await client.messages.create(
                model=request.model,
                max_tokens=request.max_output_tokens,
                messages=[{"role": "user", "content": request.input}],
                system=request.instructions if request.instructions is not None else omit,
                output_config={"effort": effort} if effort is not None else omit,
            )
        except APIStatusError as exc:
            raise _status_error(exc) from exc
        except APIConnectionError as exc:
            raise _connection_error(exc) from exc
        return _to_response(message)


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
        omitted_block_types=tuple(omitted),
    )


def _status_error(exc: APIStatusError) -> ModelCallError:
    code = exc.status_code
    retry_after = parse_retry_after(exc.response.headers)
    match code:
        case 401:
            kind = FailureKind.AUTHENTICATION
        case 402:
            kind = FailureKind.BILLING
        case 403:
            kind = FailureKind.PERMISSION
        case 409:
            kind = FailureKind.CONFLICT
        case 429:
            # A spend-cap 429 carries no retry-after and keeps failing (API errors docs).
            limited = retry_after is not None
            kind = FailureKind.RATE_LIMITED if limited else FailureKind.QUOTA_EXHAUSTED
        case 503:
            kind = FailureKind.UNAVAILABLE
        case 529:
            kind = FailureKind.OVERLOADED
        case _ if code >= 500:
            kind = FailureKind.SERVER_ERROR  # 500, 504 and others: may have been processed
        case _:
            kind = FailureKind.INVALID_REQUEST  # 400, 404, 413, 422 and other 4xx
    message = f"HTTP {code} {exc.type or 'error'}"
    detail = _provider_message(exc.body)
    if detail:
        message = f"{message}: {detail}"
    return ModelCallError(
        kind,
        message,
        provider_request_id=exc.request_id,
        retry_after_s=retry_after if kind is FailureKind.RATE_LIMITED else None,
    )


def _connection_error(exc: APIConnectionError) -> ModelCallError:
    cause = exc.__cause__
    if isinstance(cause, _NOT_SENT):
        return ModelCallError(
            FailureKind.CONNECTION_FAILED, f"could not connect ({type(cause).__name__})"
        )
    if isinstance(exc, APITimeoutError):
        return ModelCallError(FailureKind.TIMEOUT, "request timed out after it was sent")
    name = type(cause).__name__ if cause is not None else "unknown"
    return ModelCallError(FailureKind.CONNECTION_LOST, f"connection failed after sending ({name})")


def _provider_message(body: object) -> str:
    """The API's `error.message`, truncated. The runtime redacts it before persisting."""
    if not isinstance(body, dict):
        return ""
    error = cast(dict[str, Any], body).get("error")
    if not isinstance(error, dict):
        return ""
    text = cast(dict[str, Any], error).get("message")
    return text[:MAX_PROVIDER_MESSAGE_CHARS] if isinstance(text, str) else ""


def parse_retry_after(headers: httpx2.Headers) -> float | None:
    """Seconds from `retry-after-ms` or `retry-after` (seconds). HTTP-date values are ignored."""
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            value = float(raw) * scale
        except ValueError:
            continue
        if value >= 0:
            return value
    return None
