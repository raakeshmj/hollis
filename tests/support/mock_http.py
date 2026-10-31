"""A scripted httpx2 transport that records requests. No network access is possible."""

import json
from collections.abc import Callable
from typing import Any

import httpx2

type Reply = httpx2.Response | Exception | Callable[[httpx2.Request], httpx2.Response]


class MockHTTP:
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
