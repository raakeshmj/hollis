"""Offline tests of the Anthropic adapter against a real SDK client over a mock transport.

These verify request construction, response mapping and failure classification against the
current API schema. They do not verify live account access or actual provider behaviour; see
tests/live/ for the opt-in live check.
"""

from collections.abc import Callable

import httpx2
import pytest
from anthropic import AsyncAnthropic
from pydantic import SecretStr

from hollis.adapters.anthropic_provider import (
    ANTHROPIC_CAPABILITIES,
    MAX_NONSTREAMING_OUTPUT_TOKENS,
    AnthropicProvider,
    AnthropicSettings,
    parse_retry_after,
)
from hollis.adapters.credentials import MissingCredential, require_env_secret
from hollis.runtime.model import (
    CallOutcome,
    FailureKind,
    ModelCallError,
    ModelProvider,
    ModelRequest,
    StopReason,
)
from tests.support.anthropic import TEST_KEY, MockAnthropic, api_error, fixture, ok

MODEL = "claude-opus-5-5"


def _request(**overrides: object) -> ModelRequest:
    fields: dict[str, object] = {
        "operation_id": "run/model/0",
        "model": MODEL,
        "input": "Say hello.",
        "max_output_tokens": 1024,
        "timeout_s": 42.0,
    }
    return ModelRequest.model_validate(fields | overrides)


async def _fail(mock: MockAnthropic, request: ModelRequest | None = None) -> ModelCallError:
    with pytest.raises(ModelCallError) as exc_info:
        await mock.provider().generate(request or _request())
    return exc_info.value


# --- capabilities and credentials ----------------------------------------------------------


def test_declares_no_provider_side_deduplication() -> None:
    provider: ModelProvider = MockAnthropic().provider()  # checked by pyright
    assert provider.capabilities == ANTHROPIC_CAPABILITIES
    assert not provider.capabilities.deduplicates_requests


def test_missing_api_key_raises_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(MissingCredential, match="ANTHROPIC_API_KEY"):
        AnthropicProvider.from_env()


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_credentials_count_as_missing(value: str) -> None:
    with pytest.raises(MissingCredential):
        require_env_secret("ANTHROPIC_API_KEY", {"ANTHROPIC_API_KEY": value})


def test_loaded_credential_is_hidden_in_reprs() -> None:
    secret = require_env_secret("ANTHROPIC_API_KEY", {"ANTHROPIC_API_KEY": TEST_KEY})
    assert TEST_KEY not in repr(secret)
    assert TEST_KEY not in str(secret)
    assert secret.get_secret_value() == TEST_KEY


# --- request construction ------------------------------------------------------------------


async def test_request_is_a_minimal_messages_call() -> None:
    mock = MockAnthropic(ok())
    await mock.provider().generate(_request())

    (sent,) = mock.requests
    assert (sent.method, sent.url.path) == ("POST", "/v1/messages")
    assert mock.body() == {
        "model": MODEL,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": "Say hello."}],
    }
    # SDK 2.x sends API keys as a bearer token.
    assert sent.headers["authorization"] == f"Bearer {TEST_KEY}"
    assert sent.headers["anthropic-version"]
    assert sent.headers["x-stainless-retry-count"] == "0"


async def test_instructions_become_the_system_prompt_and_effort_is_opt_in() -> None:
    mock = MockAnthropic(ok())
    await mock.provider(AnthropicSettings(effort="low")).generate(
        _request(instructions="Be terse.")
    )
    body = mock.body()
    assert body["system"] == "Be terse."
    assert body["output_config"] == {"effort": "low"}
    assert not {"thinking", "temperature", "top_p", "top_k"} & body.keys()


async def test_per_attempt_timeout_is_applied_to_the_http_request() -> None:
    mock = MockAnthropic(ok())
    await mock.provider().generate(_request(timeout_s=7.5))
    timeouts = mock.requests[0].extensions["timeout"]
    assert timeouts["read"] == 7.5


async def test_output_token_limit_beyond_the_non_streaming_bound_is_rejected_locally() -> None:
    mock = MockAnthropic()
    error = await _fail(mock, _request(max_output_tokens=MAX_NONSTREAMING_OUTPUT_TOKENS + 1))
    assert error.kind is FailureKind.INVALID_REQUEST
    assert mock.calls == 0


# --- response mapping ----------------------------------------------------------------------


async def test_text_blocks_are_joined_and_metadata_captured() -> None:
    mock = MockAnthropic(ok(request_id="req_011ABC"))
    response = await mock.provider().generate(_request())

    assert response.content == "Hello, world."
    assert response.stop_reason is StopReason.END_TURN
    assert response.served_model == MODEL
    assert response.provider_request_id == "req_011ABC"
    assert response.omitted_block_types == ()
    assert response.usage is not None
    assert response.usage.model_dump() == {
        "input_tokens": 25,
        "output_tokens": 40,
        "cache_read_input_tokens": 12,
        "cache_creation_input_tokens": 0,
        "thinking_tokens": 28,
    }


async def test_refusal_is_a_response_not_an_error() -> None:
    mock = MockAnthropic(ok(fixture("message_refusal.json")))
    response = await mock.provider().generate(_request())
    assert (response.content, response.stop_reason) == ("", StopReason.REFUSAL)
    assert response.usage is not None
    assert response.usage.thinking_tokens is None


async def test_unexpected_blocks_are_reported_not_dropped_silently() -> None:
    mock = MockAnthropic(ok(fixture("message_max_tokens_with_tool_use.json")))
    response = await mock.provider().generate(_request())
    assert response.content == "Partial ans"
    assert response.stop_reason is StopReason.MAX_TOKENS
    assert response.omitted_block_types == ("tool_use",)


@pytest.mark.parametrize(
    ("api_value", "expected"),
    [
        ("end_turn", StopReason.END_TURN),
        ("max_tokens", StopReason.MAX_TOKENS),
        ("stop_sequence", StopReason.STOP_SEQUENCE),
        ("refusal", StopReason.REFUSAL),
        ("model_context_window_exceeded", StopReason.CONTEXT_WINDOW_EXCEEDED),
        ("pause_turn", StopReason.OTHER),
        ("tool_use", StopReason.OTHER),
        (None, StopReason.OTHER),
    ],
)
async def test_stop_reason_mapping(api_value: str | None, expected: StopReason) -> None:
    body = fixture("message_thinking_and_text.json") | {"stop_reason": api_value}
    response = await MockAnthropic(ok(body)).provider().generate(_request())
    assert response.stop_reason is expected


# --- failure classification ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "error_type", "headers", "kind"),
    [
        (400, "invalid_request_error", {}, FailureKind.INVALID_REQUEST),
        (401, "authentication_error", {}, FailureKind.AUTHENTICATION),
        (402, "billing_error", {}, FailureKind.BILLING),
        (403, "permission_error", {}, FailureKind.PERMISSION),
        (404, "not_found_error", {}, FailureKind.INVALID_REQUEST),
        (409, "conflict_error", {}, FailureKind.CONFLICT),
        (413, "request_too_large", {}, FailureKind.INVALID_REQUEST),
        (422, "invalid_request_error", {}, FailureKind.INVALID_REQUEST),
        (429, "rate_limit_error", {"retry-after": "12"}, FailureKind.RATE_LIMITED),
        (429, "rate_limit_error", {}, FailureKind.QUOTA_EXHAUSTED),
        (500, "api_error", {}, FailureKind.SERVER_ERROR),
        (503, "api_error", {}, FailureKind.UNAVAILABLE),
        (504, "timeout_error", {}, FailureKind.SERVER_ERROR),
        (529, "overloaded_error", {}, FailureKind.OVERLOADED),
    ],
)
async def test_http_errors_are_classified_by_status(
    status: int, error_type: str, headers: dict[str, str], kind: FailureKind
) -> None:
    mock = MockAnthropic(api_error(status, error_type, headers=headers, request_id="req_err_1"))
    error = await _fail(mock)

    assert error.kind is kind
    assert error.provider_request_id == "req_err_1"
    assert error.message.startswith(f"HTTP {status} {error_type}")
    assert mock.calls == 1  # the SDK made no retries of its own


@pytest.mark.parametrize(
    ("headers", "expected"),
    [({"retry-after": "12"}, 12.0), ({"retry-after-ms": "1500", "retry-after": "9"}, 1.5)],
)
async def test_rate_limit_retry_after_is_captured(headers: dict[str, str], expected: float) -> None:
    error = await _fail(MockAnthropic(api_error(429, "rate_limit_error", headers=headers)))
    assert (error.kind, error.retry_after_s) == (FailureKind.RATE_LIMITED, expected)


def test_retry_after_parsing_ignores_unusable_values() -> None:
    assert (
        parse_retry_after(httpx2.Headers({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})) is None
    )
    assert parse_retry_after(httpx2.Headers({"retry-after": "-1"})) is None
    assert parse_retry_after(httpx2.Headers({})) is None


def _raises(exc_type: type[httpx2.TransportError]) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc_type("simulated", request=request)

    return handler


@pytest.mark.parametrize(
    ("transport_error", "kind"),
    [
        (httpx2.ConnectError, FailureKind.CONNECTION_FAILED),
        (httpx2.ConnectTimeout, FailureKind.CONNECTION_FAILED),
        (httpx2.PoolTimeout, FailureKind.CONNECTION_FAILED),
        (httpx2.ReadTimeout, FailureKind.TIMEOUT),
        (httpx2.WriteTimeout, FailureKind.TIMEOUT),
        (httpx2.RemoteProtocolError, FailureKind.CONNECTION_LOST),
        (httpx2.ReadError, FailureKind.CONNECTION_LOST),
    ],
)
async def test_transport_failures_distinguish_not_sent_from_ambiguous(
    transport_error: type[httpx2.TransportError], kind: FailureKind
) -> None:
    mock = MockAnthropic(_raises(transport_error))
    error = await _fail(mock)
    assert error.kind is kind
    assert mock.calls == 1
    expected_outcome = (
        CallOutcome.NOT_PROCESSED
        if kind is FailureKind.CONNECTION_FAILED
        else CallOutcome.AMBIGUOUS
    )
    assert error.outcome is expected_outcome


async def test_provider_error_message_is_truncated_and_excludes_the_api_key() -> None:
    mock = MockAnthropic(api_error(400, "invalid_request_error", "x" * 1000))
    error = await _fail(mock)
    assert len(error.message) < 400
    assert TEST_KEY not in error.message


async def test_injected_client_retries_are_disabled_per_call() -> None:
    mock = MockAnthropic(api_error(529, "overloaded_error"))
    client = AsyncAnthropic(api_key=TEST_KEY, max_retries=5, http_client=mock.http_client())
    with pytest.raises(ModelCallError):
        await AnthropicProvider(client).generate(_request())
    assert mock.calls == 1


async def test_provider_closes_its_client() -> None:
    mock = MockAnthropic(ok())
    async with mock.provider() as provider:
        await provider.generate(_request())
    # A closed client cannot send. The SDK reports this as a connection error, which is
    # classified conservatively (ambiguous) rather than guessed to be safe.
    with pytest.raises(ModelCallError):
        await provider.generate(_request())
    assert mock.calls == 1


def test_secret_api_key_is_not_exposed_by_the_provider() -> None:
    provider = AnthropicProvider.from_api_key(SecretStr(TEST_KEY))
    assert TEST_KEY not in repr(provider)
