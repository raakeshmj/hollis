"""Offline harness for the Anthropic adapter: a real AsyncAnthropic over httpx2.MockTransport.

No network access is possible: every request is answered by the scripted handler, and the
handler fails the test loudly if the script runs out.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
from pydantic import SecretStr

from hollis.adapters.anthropic_provider import AnthropicProvider, AnthropicSettings

FIXTURES = Path(__file__).parents[1] / "adapters" / "fixtures" / "anthropic"
TEST_KEY = "sk-ant-api03-TESTKEYvalue-not-real-0123456789abcdef"

type Reply = httpx2.Response | Exception | Callable[[httpx2.Request], httpx2.Response]


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def ok(
    body: dict[str, Any] | None = None, *, request_id: str = "req_011CSHoEeqs5C35K2UUqR7Fy"
) -> httpx2.Response:
    return httpx2.Response(
        200,
        json=body or fixture("message_thinking_and_text.json"),
        headers={"request-id": request_id},
    )


def api_error(
    status: int,
    error_type: str,
    message: str = "error message from the API",
    *,
    headers: dict[str, str] | None = None,
    request_id: str = "req_011ERRORrequestid000000",
) -> httpx2.Response:
    return httpx2.Response(
        status,
        json={
            "type": "error",
            "error": {"type": error_type, "message": message},
            "request_id": request_id,
        },
        headers={"request-id": request_id, **(headers or {})},
    )


class MockAnthropic:
    def __init__(self, *replies: Reply) -> None:
        self._replies = list(replies)
        self.requests: list[httpx2.Request] = []

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self._replies:
            raise AssertionError(f"unexpected extra request to {request.url}")
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            return reply(request)
        return reply

    @property
    def calls(self) -> int:
        return len(self.requests)

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)

    def http_client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self._handle))

    def provider(self, settings: AnthropicSettings | None = None) -> AnthropicProvider:
        return AnthropicProvider.from_api_key(
            SecretStr(TEST_KEY), settings, http_client=self.http_client()
        )
