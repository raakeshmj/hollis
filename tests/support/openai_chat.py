"""Offline harness for the OpenAI-compatible adapter."""

import json
from pathlib import Path
from typing import Any

import httpx2
from pydantic import SecretStr

from hollis.adapters.config import AuthConfig, EndpointConfig, NoAuth, OpenAIChatOptions
from hollis.adapters.openai_chat import OpenAIChatProvider
from hollis.runtime.model import DeploymentDescriptor, Locality
from tests.support.http import MockHTTP

FIXTURES = Path(__file__).parents[1] / "adapters" / "fixtures" / "openai_chat"
LOCAL_URL = "http://127.0.0.1:8000/v1"
TEST_TOKEN = "local-gateway-token-0123456789"
DESCRIPTOR = DeploymentDescriptor(
    deployment="local-qwen",
    provider="vllm",
    protocol="openai-chat-completions",
    model="Qwen/Qwen3-8B",
    endpoint="http://127.0.0.1:8000",
    locality=Locality.LOCAL,
    deduplicates_requests=False,
)


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def ok(name: str = "openai_reference.json", *, request_id: str | None = None) -> httpx2.Response:
    headers = {"x-request-id": request_id} if request_id else {}
    return httpx2.Response(200, json=fixture(name), headers=headers)


def error(status: int, body: object, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(status, json=body, headers=headers or {})


class MockChatServer(MockHTTP):
    def provider(
        self,
        options: OpenAIChatOptions | None = None,
        *,
        base_url: str = LOCAL_URL,
        auth: AuthConfig | None = None,
        credential: SecretStr | None = None,
        headers: dict[str, str] | None = None,
        descriptor: DeploymentDescriptor = DESCRIPTOR,
    ) -> OpenAIChatProvider:
        endpoint = EndpointConfig(base_url=base_url, auth=auth or NoAuth(), headers=headers or {})
        return OpenAIChatProvider(
            descriptor, endpoint, credential, options, http_client=self.http_client()
        )
