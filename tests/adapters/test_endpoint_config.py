from pathlib import Path

import httpx2
import pytest
from pydantic import ValidationError

from hollis.adapters._http import (
    build_async_client,
    parse_retry_after,
    provider_message,
    status_failure_kind,
    transport_failure,
)
from hollis.adapters.config import (
    BearerAuth,
    EndpointConfig,
    OpenAIChatOptions,
    TlsConfig,
)
from hollis.runtime.model import CallOutcome, FailureKind


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://host", "absolute http"),
        ("localhost:8000", "absolute http"),
        ("https://user:pw@host/v1", "embed credentials"),
        ("https://host/v1?key=abc", "query string"),
    ],
)
def test_unsafe_base_urls_are_rejected(url: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        EndpointConfig(base_url=url)


@pytest.mark.parametrize("header", ["Authorization", "x-api-key", "Cookie", "api-key"])
def test_credential_headers_must_use_auth(header: str) -> None:
    with pytest.raises(ValidationError, match="configure it via `auth`"):
        EndpointConfig(base_url="https://h", headers={header: "x"})


def test_auth_references_environment_variable_names_only() -> None:
    with pytest.raises(ValidationError):
        BearerAuth(env="not a name; rm -rf")


@pytest.mark.parametrize(
    ("url", "origin", "loopback"),
    [
        ("http://127.0.0.1:8000/v1/", "http://127.0.0.1:8000", True),
        ("http://localhost:11434/v1", "http://localhost:11434", True),
        ("http://[::1]:8080/v1", "http://[::1]:8080", True),
        ("https://inference.corp.internal/v1", "https://inference.corp.internal", False),
        ("https://api.anthropic.com", "https://api.anthropic.com", False),
    ],
)
def test_origin_and_loopback_detection(url: str, origin: str, loopback: bool) -> None:
    endpoint = EndpointConfig(base_url=url)
    assert (endpoint.origin, endpoint.is_loopback) == (origin, loopback)
    assert not endpoint.base_url.endswith("/")


def test_tls_options_are_consistent() -> None:
    with pytest.raises(ValidationError, match="client_key requires client_cert"):
        TlsConfig(client_key=Path("k.pem"))
    with pytest.raises(ValidationError, match="no effect"):
        TlsConfig(verify=False, ca_bundle=Path("ca.pem"))


def test_extra_body_cannot_override_managed_fields() -> None:
    with pytest.raises(ValidationError, match="managed fields"):
        OpenAIChatOptions(extra_body={"top_k": 50, "max_tokens": 10})
    assert OpenAIChatOptions(extra_body={"top_k": 50}).extra_body == {"top_k": 50}


async def test_clients_ignore_proxy_environment_and_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    client = build_async_client(EndpointConfig(base_url="https://h"))
    assert client.trust_env is False
    assert client.follow_redirects is False
    await client.aclose()


@pytest.mark.parametrize(
    ("status", "quota", "kind"),
    [
        (400, False, FailureKind.INVALID_REQUEST),
        (401, False, FailureKind.AUTHENTICATION),
        (402, False, FailureKind.BILLING),
        (403, False, FailureKind.PERMISSION),
        (404, False, FailureKind.INVALID_REQUEST),
        (408, False, FailureKind.UNAVAILABLE),
        (409, False, FailureKind.CONFLICT),
        (422, False, FailureKind.INVALID_REQUEST),
        (429, False, FailureKind.RATE_LIMITED),
        (429, True, FailureKind.QUOTA_EXHAUSTED),
        (500, False, FailureKind.SERVER_ERROR),
        (502, False, FailureKind.SERVER_ERROR),
        (503, False, FailureKind.UNAVAILABLE),
        (504, False, FailureKind.SERVER_ERROR),
        (529, False, FailureKind.OVERLOADED),
    ],
)
def test_status_classification(status: int, quota: bool, kind: FailureKind) -> None:
    assert status_failure_kind(status, quota_exhausted=quota) is kind


@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (httpx2.ConnectError("x"), FailureKind.CONNECTION_FAILED),
        (httpx2.ConnectTimeout("x"), FailureKind.CONNECTION_FAILED),
        (httpx2.PoolTimeout("x"), FailureKind.CONNECTION_FAILED),
        (httpx2.ProxyError("x"), FailureKind.CONNECTION_FAILED),
        (httpx2.ReadTimeout("x"), FailureKind.TIMEOUT),
        (httpx2.RemoteProtocolError("x"), FailureKind.CONNECTION_LOST),
        (None, FailureKind.CONNECTION_LOST),
    ],
)
def test_transport_classification(exc: BaseException | None, kind: FailureKind) -> None:
    error = transport_failure(exc)
    assert error.kind is kind
    assert (error.outcome is CallOutcome.NOT_PROCESSED) == (kind is FailureKind.CONNECTION_FAILED)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"error": {"message": "openai style"}}, "openai style"),
        ({"error": "plain string"}, "plain string"),
        ({"object": "error", "message": "top level"}, "top level"),
        ({"detail": "fastapi style"}, "fastapi style"),
        ({"error": {"message": "x" * 1000}}, "x" * 300),
        ("not a dict", ""),
    ],
)
def test_provider_messages_from_common_error_shapes(body: object, expected: str) -> None:
    assert provider_message(body) == expected


def test_retry_after_parsing() -> None:
    assert parse_retry_after(httpx2.Headers({"retry-after": "12"})) == 12.0
    assert parse_retry_after(httpx2.Headers({"retry-after-ms": "1500", "retry-after": "9"})) == 1.5
    assert (
        parse_retry_after(httpx2.Headers({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})) is None
    )
    assert parse_retry_after(httpx2.Headers({})) is None
