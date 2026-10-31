"""Offline tests of the OpenAI-compatible adapter over a mock transport.

Fixtures are representative of documented response shapes (see fixtures/openai_chat/README.md);
they do not establish compatibility with any particular server.
"""

from collections.abc import Callable

import httpx2
import pytest
from pydantic import SecretStr

from hollis.adapters.config import BearerAuth, HeaderAuth, OpenAIChatOptions
from hollis.runtime.model import (
    CallOutcome,
    FailureKind,
    ModelCallError,
    ModelProvider,
    ModelRequest,
    StopReason,
)
from tests.support.openai_chat import (
    DESCRIPTOR,
    TEST_TOKEN,
    MockChatServer,
    error,
    ok,
)


def _request(**overrides: object) -> ModelRequest:
    fields: dict[str, object] = {
        "operation_id": "run/model/0",
        "model": "Qwen/Qwen3-8B",
        "input": "Say hello.",
        "max_output_tokens": 256,
        "timeout_s": 30.0,
    }
    return ModelRequest.model_validate(fields | overrides)


async def _fail(mock: MockChatServer, **provider_kwargs: object) -> ModelCallError:
    with pytest.raises(ModelCallError) as exc_info:
        await mock.provider(**provider_kwargs).generate(_request())  # type: ignore[arg-type]  # test passthrough
    return exc_info.value


def test_satisfies_the_provider_port() -> None:
    provider: ModelProvider = MockChatServer().provider()  # checked by pyright
    assert provider.descriptor == DESCRIPTOR


# --- request construction ------------------------------------------------------------------


async def test_default_request_is_minimal_and_keyless() -> None:
    mock = MockChatServer(ok())
    await mock.provider().generate(_request())

    (sent,) = mock.requests
    assert (sent.method, str(sent.url)) == ("POST", "http://127.0.0.1:8000/v1/chat/completions")
    assert mock.body() == {
        "model": "Qwen/Qwen3-8B",
        "messages": [{"role": "user", "content": "Say hello."}],
        "max_tokens": 256,
    }
    assert "authorization" not in sent.headers
    assert sent.extensions["timeout"]["read"] == 30.0


async def test_options_shape_the_request_explicitly() -> None:
    mock = MockChatServer(ok())
    options = OpenAIChatOptions(
        token_limit_param="max_completion_tokens",
        system_role="developer",
        reasoning_effort="low",
        extra_body={"top_k": 20, "chat_template_kwargs": {"enable_thinking": False}},
    )
    await mock.provider(options).generate(_request(instructions="Be terse."))
    assert mock.body() == {
        "model": "Qwen/Qwen3-8B",
        "messages": [
            {"role": "developer", "content": "Be terse."},
            {"role": "user", "content": "Say hello."},
        ],
        "max_completion_tokens": 256,
        "reasoning_effort": "low",
        "top_k": 20,
        "chat_template_kwargs": {"enable_thinking": False},
    }


async def test_bearer_and_header_auth() -> None:
    mock = MockChatServer(ok(), ok())
    secret = SecretStr(TEST_TOKEN)
    await mock.provider(auth=BearerAuth(env="T"), credential=secret).generate(_request())
    await mock.provider(auth=HeaderAuth(header="api-key", env="T"), credential=secret).generate(
        _request()
    )
    bearer, header = mock.requests
    assert bearer.headers["authorization"] == f"Bearer {TEST_TOKEN}"
    assert header.headers["api-key"] == TEST_TOKEN
    assert "authorization" not in header.headers


async def test_environment_variables_are_never_consulted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "http://attacker.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-hostile-environment-key-0000")
    monkeypatch.setenv("OPENAI_ORG_ID", "org-hostile")
    mock = MockChatServer(ok())
    await mock.provider().generate(_request())
    sent = mock.requests[0]
    assert sent.url.host == "127.0.0.1"
    assert not {"authorization", "openai-organization"} & set(sent.headers.keys())


# --- response mapping ----------------------------------------------------------------------


async def test_openai_reference_response() -> None:
    response = await MockChatServer(ok(request_id="req_abc")).provider().generate(_request())
    assert response.content == "Hello."
    assert response.stop_reason is StopReason.END_TURN
    assert (response.served_model, response.provider_request_id, response.provider_response_id) == (
        "gpt-5.5",
        "req_abc",
        "chatcmpl-B9MHDbslfkBeAs8l4bebGdFOJ6PeG",
    )
    assert response.usage is not None
    assert response.usage.model_dump() == {
        "input_tokens": 12,
        "output_tokens": 9,
        "cache_read_input_tokens": 4,
        "cache_creation_input_tokens": None,
        "thinking_tokens": 6,
    }


async def test_reasoning_content_is_not_persisted_as_output() -> None:
    response = (
        await MockChatServer(ok("reasoning_content_server.json")).provider().generate(_request())
    )
    assert response.content == "Hi there."
    assert "greeting" not in response.content


async def test_content_parts_without_usage() -> None:
    response = await MockChatServer(ok("minimal_no_usage.json")).provider().generate(_request())
    assert response.content == "Part one, part two."
    assert response.stop_reason is StopReason.MAX_TOKENS
    assert response.usage is None


async def test_refusal_is_a_response() -> None:
    response = await MockChatServer(ok("refusal.json")).provider().generate(_request())
    assert (response.content, response.stop_reason) == ("", StopReason.REFUSAL)


@pytest.mark.parametrize(
    ("finish", "stop"),
    [
        ("stop", StopReason.END_TURN),
        ("length", StopReason.MAX_TOKENS),
        ("content_filter", StopReason.CONTENT_FILTERED),
        ("tool_calls", StopReason.TOOL_USE),
        (None, StopReason.OTHER),
    ],
)
async def test_finish_reason_mapping(finish: str | None, stop: StopReason) -> None:
    body = {"choices": [{"message": {"content": "x"}, "finish_reason": finish}]}
    response = await MockChatServer(httpx2.Response(200, json=body)).provider().generate(_request())
    assert response.stop_reason is stop


@pytest.mark.parametrize(
    "reply",
    [
        httpx2.Response(200, text="<html>proxy page</html>"),
        httpx2.Response(200, json={"choices": []}),
        httpx2.Response(200, json={"object": "list", "data": []}),
    ],
    ids=["not-json", "no-choices", "wrong-object"],
)
async def test_unusable_success_response_is_ambiguous(reply: httpx2.Response) -> None:
    error_ = await _fail(MockChatServer(reply))
    assert error_.kind is FailureKind.INVALID_RESPONSE
    assert error_.outcome is CallOutcome.AMBIGUOUS


# --- failure classification ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "headers", "kind", "retry_after"),
    [
        (
            400,
            {"error": {"message": "bad", "type": "invalid_request_error"}},
            {},
            FailureKind.INVALID_REQUEST,
            None,
        ),
        (
            400,
            {"object": "error", "message": "vLLM style", "type": "BadRequestError", "code": 400},
            {},
            FailureKind.INVALID_REQUEST,
            None,
        ),
        (
            401,
            {"error": {"message": "no", "type": "invalid_api_key"}},
            {},
            FailureKind.AUTHENTICATION,
            None,
        ),
        (404, {"error": "model not found"}, {}, FailureKind.INVALID_REQUEST, None),
        (
            429,
            {"error": {"message": "slow down", "type": "requests"}},
            {"retry-after": "2"},
            FailureKind.RATE_LIMITED,
            2.0,
        ),
        (
            429,
            {
                "error": {
                    "message": "quota",
                    "type": "insufficient_quota",
                    "code": "insufficient_quota",
                }
            },
            {},
            FailureKind.QUOTA_EXHAUSTED,
            None,
        ),
        (500, {"error": {"message": "boom"}}, {}, FailureKind.SERVER_ERROR, None),
        (
            503,
            {"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}},
            {"retry-after": "5"},
            FailureKind.UNAVAILABLE,
            5.0,
        ),
    ],
)
async def test_http_errors_are_classified(
    status: int,
    body: object,
    headers: dict[str, str],
    kind: FailureKind,
    retry_after: float | None,
) -> None:
    mock = MockChatServer(error(status, body, {**headers, "x-request-id": "req_e"}))
    failure = await _fail(mock)
    assert (failure.kind, failure.retry_after_s, failure.provider_request_id) == (
        kind,
        retry_after,
        "req_e",
    )
    assert failure.message.startswith(f"HTTP {status}")
    assert mock.calls == 1


def _raises(exc_type: type[httpx2.TransportError]) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc_type("simulated", request=request)

    return handler


@pytest.mark.parametrize(
    ("exc_type", "kind"),
    [
        (httpx2.ConnectError, FailureKind.CONNECTION_FAILED),
        (httpx2.ConnectTimeout, FailureKind.CONNECTION_FAILED),
        (httpx2.ReadTimeout, FailureKind.TIMEOUT),
        (httpx2.RemoteProtocolError, FailureKind.CONNECTION_LOST),
    ],
)
async def test_transport_failures(exc_type: type[httpx2.TransportError], kind: FailureKind) -> None:
    assert (await _fail(MockChatServer(_raises(exc_type)))).kind is kind


async def test_provider_message_is_passed_through_for_runtime_redaction() -> None:
    """The adapter doesn't redact; the runtime does, before anything is persisted or logged
    (asserted end to end in tests/gateway/test_gateway_runtime.py)."""
    mock = MockChatServer(error(401, {"error": {"message": f"invalid token {TEST_TOKEN}"}}))
    failure = await _fail(mock, auth=BearerAuth(env="T"), credential=SecretStr(TEST_TOKEN))
    assert failure.kind is FailureKind.AUTHENTICATION
    assert TEST_TOKEN in failure.message
