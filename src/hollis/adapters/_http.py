"""Shared HTTP plumbing for model adapters, built on `httpx2` (the stack the official SDKs use).

- Clients never read proxy or certificate environment variables (`trust_env=False`) and never
  follow redirects, so traffic goes only where the endpoint configuration says.
- Transport failures are classified by exception type: failures before a request could be sent
  are `not_processed`; anything after sending is ambiguous (ADR-0007).
"""

import logging
import ssl
from typing import Any, cast

import httpx2

from hollis.adapters.config import EndpointConfig
from hollis.runtime.model import CallOutcome, FailureKind, ModelCallError

log = logging.getLogger("hollis.adapters")

MAX_PROVIDER_MESSAGE_CHARS = 300
_NOT_SENT = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout, httpx2.ProxyError)


def build_async_client(endpoint: EndpointConfig) -> httpx2.AsyncClient:
    tls = endpoint.tls
    verify: ssl.SSLContext | bool
    if not tls.verify:
        log.warning("TLS verification is disabled for %s", endpoint.origin)
        verify = False
    else:
        verify = ssl.create_default_context(
            cafile=str(tls.ca_bundle) if tls.ca_bundle is not None else None
        )
        if tls.client_cert is not None:
            verify.load_cert_chain(
                str(tls.client_cert), str(tls.client_key) if tls.client_key else None
            )
    return httpx2.AsyncClient(
        verify=verify,
        proxy=endpoint.proxy,
        trust_env=False,
        follow_redirects=False,
        timeout=httpx2.Timeout(600.0, connect=10.0),
    )


def transport_failure(cause: BaseException | None) -> ModelCallError:
    if isinstance(cause, _NOT_SENT):
        return ModelCallError(
            FailureKind.CONNECTION_FAILED, f"could not connect ({type(cause).__name__})"
        )
    if isinstance(cause, httpx2.TimeoutException):
        return ModelCallError(FailureKind.TIMEOUT, "request timed out after it was sent")
    name = type(cause).__name__ if cause is not None else "unknown"
    return ModelCallError(FailureKind.CONNECTION_LOST, f"connection failed after sending ({name})")


def status_failure_kind(status: int, *, quota_exhausted: bool = False) -> FailureKind:
    match status:
        case 401:
            return FailureKind.AUTHENTICATION
        case 402:
            return FailureKind.BILLING
        case 403:
            return FailureKind.PERMISSION
        case 408:
            return FailureKind.UNAVAILABLE  # the server gave up waiting for the request
        case 409:
            return FailureKind.CONFLICT
        case 429:
            return FailureKind.QUOTA_EXHAUSTED if quota_exhausted else FailureKind.RATE_LIMITED
        case 503:
            return FailureKind.UNAVAILABLE  # e.g. llama.cpp while the model is loading
        case 529:
            return FailureKind.OVERLOADED
        case _ if status >= 500:
            return FailureKind.SERVER_ERROR  # 500, 502, 504: the request may have been processed
        case _:
            return FailureKind.INVALID_REQUEST


def status_error(
    status: int,
    body: object,
    headers: httpx2.Headers,
    *,
    error_type: str | None = None,
    request_id: str | None = None,
    quota_exhausted: bool = False,
) -> ModelCallError:
    kind = status_failure_kind(status, quota_exhausted=quota_exhausted)
    message = f"HTTP {status} {error_type or 'error'}"
    detail = provider_message(body)
    if detail:
        message = f"{message}: {detail}"
    retry_after = parse_retry_after(headers)
    retryable_now = kind.retryable and kind.outcome is CallOutcome.NOT_PROCESSED
    return ModelCallError(
        kind,
        message,
        provider_request_id=request_id,
        retry_after_s=retry_after if retryable_now else None,
    )


def provider_message(body: object) -> str:
    """The error message from common error body shapes, truncated. Redacted by the runtime."""
    if not isinstance(body, dict):
        return ""
    data = cast(dict[str, Any], body)
    error = data.get("error")
    candidates: list[object] = []
    if isinstance(error, dict):
        candidates.append(cast(dict[str, Any], error).get("message"))
    candidates += [error, data.get("message"), data.get("detail")]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate[:MAX_PROVIDER_MESSAGE_CHARS]
    return ""


def error_code(body: object) -> str | None:
    """`error.code` from an OpenAI-style error body, if present."""
    if isinstance(body, dict):
        error = cast(dict[str, Any], body).get("error")
        if isinstance(error, dict):
            code = cast(dict[str, Any], error).get("code")
            return code if isinstance(code, str) else None
    return None


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
