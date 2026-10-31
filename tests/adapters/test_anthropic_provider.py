"""Offline tests of the Anthropic adapter against a real SDK client over a mock transport.

These verify request construction, response mapping and failure classification against the
current API schema. They do not verify live account access or actual provider behaviour; see
tests/live/ for the opt-in live check.
"""

from collections.abc import Callable

import httpx2
import pytest
from anthropic import AsyncAnthropic

from hollis.adapters._http import parse_retry_after
from hollis.adapters.anthropic_messages import (
    MAX_NONSTREAMING_OUTPUT_TOKENS,
    AnthropicMessagesProvider,
)
from hollis.adapters.config import AnthropicMessagesOptions, HeaderAuth, NoAuth
from hollis.adapters.credentials import MissingCredential, require_env_secret
from hollis.runtime.model import (
    CallOutcome,
    FailureKind,
    ModelCallError,
    ModelProvider,
    ModelRequest,
    StopReason,
)
from tests.support.anthropic import DESCRIPTOR, TEST_KEY, MockAnthropic, api_error, fixture, ok

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
    assert provider.descriptor == DESCRIPTOR
    assert not provider.descriptor.deduplicates_requests


def test_missing_api_key_raises_a_clear_error() -> None:
    with pytest.raises(MissingCredential, match="ANTHROPIC_API_KEY"):
        require_env_secret("ANTHROPIC_API_KEY", {})


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
    await mock.provider(AnthropicMessagesOptions(effort="low")).generate(
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


async def test_tool_use_in_a_truncated_response_is_still_surfaced() -> None:
    mock = MockAnthropic(ok(fixture("message_max_tokens_with_tool_use.json")))
    response = await mock.provider().generate(_request())
    assert response.content == "Partial ans"
    assert response.stop_reason is StopReason.MAX_TOKENS
    assert [c.name for c in response.tool_calls] == [
        "unexpected"
    ]  # the runtime rejects unknown tools
    assert response.omitted_block_types == ()


async def test_unknown_block_types_are_reported_not_dropped_silently() -> None:
    body = fixture("message_thinking_and_text.json")
    body["content"] = [*body["content"], {"type": "container_upload", "file_id": "file_1"}]
    response = await MockAnthropic(ok(body)).provider().generate(_request())
    assert response.omitted_block_types == ("container_upload",)


@pytest.mark.parametrize(
    ("api_value", "expected"),
    [
        ("end_turn", StopReason.END_TURN),
        ("max_tokens", StopReason.MAX_TOKENS),
        ("stop_sequence", StopReason.STOP_SEQUENCE),
        ("refusal", StopReason.REFUSAL),
        ("model_context_window_exceeded", StopReason.CONTEXT_WINDOW_EXCEEDED),
        ("pause_turn", StopReason.OTHER),
        ("tool_use", StopReason.TOOL_USE),
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
        (429, "rate_limit_error", {}, FailureKind.RATE_LIMITED),  # quota rule is opt-in
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
    client = AsyncAnthropic(
        api_key=TEST_KEY,
        base_url="https://api.anthropic.com",
        max_retries=5,
        http_client=mock.http_client(),
    )
    with pytest.raises(ModelCallError):
        await AnthropicMessagesProvider(DESCRIPTOR, client).generate(_request())
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
    provider = MockAnthropic().provider()
    assert TEST_KEY not in repr(provider)


# --- explicit endpoints, auth modes and hostile environments --------------------------------


async def test_keyless_endpoint_sends_no_credential() -> None:
    mock = MockAnthropic(ok())
    provider = mock.provider(base_url="http://127.0.0.1:11434", auth=NoAuth(), credential=None)
    await provider.generate(_request())
    sent = mock.requests[0]
    assert "authorization" not in sent.headers
    assert "x-api-key" not in sent.headers
    assert str(sent.url) == "http://127.0.0.1:11434/v1/messages"


async def test_header_auth_sends_the_secret_only_in_the_named_header() -> None:
    mock = MockAnthropic(ok())
    provider = mock.provider(auth=HeaderAuth(header="x-api-key", env="K"))
    await provider.generate(_request())
    sent = mock.requests[0]
    assert sent.headers["x-api-key"] == TEST_KEY
    assert "authorization" not in sent.headers


async def test_version_override_and_extra_headers_are_sent() -> None:
    mock = MockAnthropic(ok())
    provider = mock.provider(
        AnthropicMessagesOptions(anthropic_version="2023-06-01"), headers={"x-team": "research"}
    )
    await provider.generate(_request())
    assert mock.requests[0].headers["anthropic-version"] == "2023-06-01"
    assert mock.requests[0].headers["x-team"] == "research"


async def test_sdk_environment_variables_cannot_redirect_or_inject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://attacker.invalid")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-HOSTILE-environment-key-000000")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "hostile-auth-token-value-0000")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "X-Leak: 1\nX-Other: 2")
    mock = MockAnthropic(ok(), ok())

    await mock.provider().generate(_request())
    keyless = mock.provider(base_url="http://127.0.0.1:8000", auth=NoAuth(), credential=None)
    await keyless.generate(_request())

    configured, local = mock.requests
    assert configured.url.host == "api.anthropic.com"
    assert configured.headers["authorization"] == f"Bearer {TEST_KEY}"
    assert local.url.host == "127.0.0.1"
    assert "authorization" not in local.headers
    for sent in (configured, local):
        assert "x-leak" not in sent.headers
        assert "x-other" not in sent.headers


async def test_quota_classification_is_opt_in_per_endpoint() -> None:
    compat = await _fail(MockAnthropic(api_error(429, "rate_limit_error")))
    assert compat.kind is FailureKind.RATE_LIMITED
    first_party = MockAnthropic(api_error(429, "rate_limit_error"))
    with pytest.raises(ModelCallError) as exc_info:
        await first_party.provider(
            AnthropicMessagesOptions(quota_on_429_without_retry_after=True)
        ).generate(_request())
    assert exc_info.value.kind is FailureKind.QUOTA_EXHAUSTED


async def test_response_id_is_captured() -> None:
    response = await MockAnthropic(ok()).provider().generate(_request())
    assert response.provider_response_id == "msg_01XFDUDYJgAACzvnptvVoYEL"
