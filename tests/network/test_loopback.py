"""End-to-end over real loopback sockets: plain HTTP and TLS with a custom CA (no internet)."""

import ssl
from pathlib import Path
from typing import Any

import pytest
import trustme

from hollis.adapters.memory_store import InMemoryRunStore
from hollis.gateway import Catalog, ModelGateway
from hollis.runtime.model import FailureKind
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import RunSpec
from hollis.runtime.state import RunStatus
from tests.support.loopback import loopback_server

BUDGET = RunBudget(max_output_tokens=64, max_attempts=1, attempt_timeout_s=10)


def _gateway(protocol: str, base_url: str, tls: dict[str, Any] | None = None) -> ModelGateway:
    suffix = "/v1" if protocol == "openai-chat-completions" else ""
    endpoint: dict[str, Any] = {"base_url": base_url + suffix}
    if tls is not None:
        endpoint["tls"] = tls
    return ModelGateway(
        Catalog.from_mapping(
            {
                "deployments": [
                    {
                        "id": "m",
                        "protocol": protocol,
                        "provider": "local-test",
                        "model": "test-model",
                        "locality": "local",
                        "endpoint": endpoint,
                    }
                ]
            }
        )
    )


async def _run(gateway: ModelGateway) -> Any:
    runner = gateway.runner(InMemoryRunStore())
    run = await runner.create(RunSpec(model="m", input="hello", budget=BUDGET))
    try:
        return await runner.execute(run.run_id)
    finally:
        await gateway.aclose()


@pytest.mark.parametrize("protocol", ["openai-chat-completions", "anthropic-messages"])
async def test_both_protocols_over_a_real_socket_without_credentials(protocol: str) -> None:
    with loopback_server() as server:
        final = await _run(_gateway(protocol, server.base_url))
    assert final.status is RunStatus.COMPLETED
    (request,) = server.requests
    assert request.path in ("/v1/chat/completions", "/v1/messages")
    assert request.body["model"] == "test-model"
    assert "authorization" not in request.headers


@pytest.fixture
def ca(tmp_path: Path) -> tuple[trustme.CA, Path]:
    authority = trustme.CA()
    path = tmp_path / "ca.pem"
    authority.cert_pem.write_to_path(str(path))
    return authority, path


def _server_tls(authority: trustme.CA) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    cert = authority.issue_cert("localhost", "127.0.0.1")
    cert.configure_cert(context)  # pyright: ignore[reportUnknownMemberType]  # trustme is partly untyped
    return context


@pytest.mark.parametrize("protocol", ["openai-chat-completions", "anthropic-messages"])
async def test_tls_with_a_custom_ca_bundle(protocol: str, ca: tuple[trustme.CA, Path]) -> None:
    authority, ca_path = ca
    with loopback_server(_server_tls(authority)) as server:
        final = await _run(_gateway(protocol, server.base_url, {"ca_bundle": str(ca_path)}))
    assert final.status is RunStatus.COMPLETED
    assert len(server.requests) == 1


@pytest.mark.parametrize("protocol", ["openai-chat-completions", "anthropic-messages"])
async def test_tls_without_the_ca_fails_before_sending(
    protocol: str, ca: tuple[trustme.CA, Path]
) -> None:
    authority, _ = ca
    with loopback_server(_server_tls(authority)) as server:
        final = await _run(_gateway(protocol, server.base_url))
    assert final.status is RunStatus.FAILED
    assert final.last_failure is not None
    assert final.last_failure.kind is FailureKind.CONNECTION_FAILED  # rejected at the handshake
    assert server.requests == []
