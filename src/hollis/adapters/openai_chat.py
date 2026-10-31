"""OpenAI Chat Completions protocol adapter over raw `httpx2` (ADR-0012).

Serves any endpoint implementing `POST {base_url}/chat/completions`: OpenAI, vLLM, Ollama,
llama.cpp, LM Studio, SGLang and enterprise gateways. Implementations differ (token-limit field,
effort handling, usage reporting, reasoning output), so behaviour is driven by the deployment's
declared options and capabilities, never guessed. Requires `pip install hollis[openai-compatible]`.

- Exactly the configured fields are sent; nothing optional is sent unless configured.
- One HTTP request per `generate` call (Hollis owns retries). No environment variables are read.
- No credential is sent unless the endpoint's auth requires one.
- Responses are parsed tolerantly (servers add fields), but a 2xx response without a usable
  choice is an `invalid_response` failure, which is ambiguous: it may have been generated.
- Reasoning output (`reasoning_content`/`reasoning`) is not persisted, matching the Anthropic
  adapter's handling of thinking blocks; reasoning token counts are recorded when reported.
"""

import json
from types import TracebackType
from typing import Any, Self

import httpx2
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from hollis.adapters._http import (
    build_async_client,
    error_code,
    error_type,
    status_error,
    transport_failure,
)
from hollis.adapters.config import (
    BearerAuth,
    EndpointConfig,
    HeaderAuth,
    NoAuth,
    OpenAIChatOptions,
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

PROTOCOL = "openai-chat-completions"

_FINISH_REASONS: dict[str, StopReason] = {
    "stop": StopReason.END_TURN,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.CONTENT_FILTERED,
}


class _Tolerant(BaseModel):
    model_config = ConfigDict(extra="allow")


class _Message(_Tolerant):
    content: str | list[dict[str, Any]] | None = None
    refusal: str | None = None


class _Choice(_Tolerant):
    message: _Message
    finish_reason: str | None = None


class _PromptDetails(_Tolerant):
    cached_tokens: int | None = None


class _CompletionDetails(_Tolerant):
    reasoning_tokens: int | None = None


class _Usage(_Tolerant):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    prompt_tokens_details: _PromptDetails | None = None
    completion_tokens_details: _CompletionDetails | None = None


class _Completion(_Tolerant):
    id: str | None = None
    model: str | None = None
    choices: list[_Choice] = Field(min_length=1)
    usage: _Usage | None = None


class OpenAIChatProvider:
    def __init__(
        self,
        descriptor: DeploymentDescriptor,
        endpoint: EndpointConfig,
        credential: SecretStr | None,
        options: OpenAIChatOptions | None = None,
        *,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._descriptor = descriptor
        self._url = f"{endpoint.base_url}/chat/completions"
        self._options = options or OpenAIChatOptions()
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

    def request_body(self, request: ModelRequest) -> dict[str, Any]:
        options = self._options
        messages: list[dict[str, str]] = []
        if request.instructions is not None:
            messages.append({"role": options.system_role, "content": request.instructions})
        messages.append({"role": "user", "content": request.input})
        body: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            options.token_limit_param: request.max_output_tokens,
        }
        if options.reasoning_effort is not None:
            body["reasoning_effort"] = options.reasoning_effort
        body.update(options.extra_body)
        return body

    async def generate(self, request: ModelRequest) -> ModelResponse:
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
        body = _json_or_none(response)
        if response.status_code >= 400:
            raise status_error(
                response.status_code,
                body,
                response.headers,
                error_type=error_type(body),
                request_id=request_id,
                quota_exhausted=error_code(body) == "insufficient_quota",
            )
        try:
            completion = _Completion.model_validate(body)
        except ValidationError as exc:
            raise ModelCallError(
                FailureKind.INVALID_RESPONSE,
                f"HTTP {response.status_code} response is not a chat completion "
                f"({exc.error_count()} validation errors)",
                provider_request_id=request_id,
            ) from exc
        return _to_response(completion, request_id)


def _require(credential: SecretStr | None) -> str:
    if credential is None:
        raise ValueError("this endpoint's auth requires a credential")
    return credential.get_secret_value()


def _json_or_none(response: httpx2.Response) -> object:
    try:
        return response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _text(content: str | list[dict[str, Any]] | None) -> str:
    if content is None or isinstance(content, str):
        return content or ""
    return "".join(
        part["text"]
        for part in content
        if part.get("type") == "text" and isinstance(part.get("text"), str)
    )


def _to_response(completion: _Completion, request_id: str | None) -> ModelResponse:
    choice = completion.choices[0]
    stop = _FINISH_REASONS.get(choice.finish_reason or "", StopReason.OTHER)
    if choice.message.refusal:
        stop = StopReason.REFUSAL
    usage = None
    if completion.usage is not None:
        u = completion.usage
        usage = Usage(
            input_tokens=u.prompt_tokens,
            output_tokens=u.completion_tokens,
            cache_read_input_tokens=u.prompt_tokens_details.cached_tokens
            if u.prompt_tokens_details
            else None,
            thinking_tokens=u.completion_tokens_details.reasoning_tokens
            if u.completion_tokens_details
            else None,
        )
    return ModelResponse(
        content=_text(choice.message.content),
        stop_reason=stop,
        usage=usage,
        served_model=completion.model,
        provider_request_id=request_id,
        provider_response_id=completion.id,
    )
