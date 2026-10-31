"""Protocol conformance: offline against the loopback server, opt-in against a real endpoint.

Live (no API key needed for local servers such as Ollama, vLLM or llama.cpp):

    HOLLIS_CONFORMANCE_PROTOCOL=openai-chat-completions \\
    HOLLIS_CONFORMANCE_BASE_URL=http://127.0.0.1:11434/v1 \\
    HOLLIS_CONFORMANCE_MODEL=qwen3:8b \\
    HOLLIS_CONFORMANCE_PROVIDER=ollama \\
    uv run pytest tests/conformance -m live -ra

Optional: HOLLIS_CONFORMANCE_AUTH_ENV (name of the env var holding a bearer token),
HOLLIS_CONFORMANCE_LOCALITY (local/private/external; default local for loopback URLs, else
external). Cloud endpoints cost money: each check makes one small request. Record results in
docs/architecture/model-compatibility.md together with the server version and model.
"""

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest

from hollis.adapters.config import EndpointConfig
from hollis.gateway import Catalog, ModelGateway
from tests.conformance.checks import CHECKS, rejects_unknown_model_as_not_processed
from tests.support.loopback import LoopbackServer, loopback_server

PROTOCOLS = ["openai-chat-completions", "anthropic-messages"]


def _deployment(
    protocol: str,
    base_url: str,
    deployment_id: str,
    wire_model: str,
    *,
    provider: str,
    locality: str,
    auth_env: str | None = None,
) -> dict[str, Any]:
    endpoint: dict[str, Any] = {"base_url": base_url}
    if auth_env:
        endpoint["auth"] = {"type": "bearer", "env": auth_env}
    return {
        "id": deployment_id,
        "protocol": protocol,
        "provider": provider,
        "model": wire_model,
        "locality": locality,
        "endpoint": endpoint,
    }


def live_catalog(environ: dict[str, str]) -> Catalog:
    """The live conformance catalog: the configured model plus a missing one."""
    base_url = environ["HOLLIS_CONFORMANCE_BASE_URL"]
    locality = environ.get(
        "HOLLIS_CONFORMANCE_LOCALITY",
        "local" if EndpointConfig(base_url=base_url).is_loopback else "external",
    )
    common: dict[str, Any] = {
        "provider": environ.get("HOLLIS_CONFORMANCE_PROVIDER", "unknown"),
        "locality": locality,
        "auth_env": environ.get("HOLLIS_CONFORMANCE_AUTH_ENV"),
    }
    protocol = environ.get("HOLLIS_CONFORMANCE_PROTOCOL", "openai-chat-completions")
    return Catalog.from_mapping(
        {
            "deployments": [
                _deployment(
                    protocol, base_url, "live", environ["HOLLIS_CONFORMANCE_MODEL"], **common
                ),
                _deployment(protocol, base_url, "live-missing", "hollis-no-such-model", **common),
            ]
        }
    )


def _offline_gateway(protocol: str, server: LoopbackServer) -> ModelGateway:
    suffix = "/v1" if protocol == "openai-chat-completions" else ""
    entries = [
        _deployment(protocol, server.base_url + suffix, m, m, provider="loopback", locality="local")
        for m in ("known-model", "missing-model")
    ]
    return ModelGateway(Catalog.from_mapping({"deployments": entries}))


@pytest.fixture
def server() -> Iterator[LoopbackServer]:
    with loopback_server() as running:
        yield running


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("check", CHECKS)
async def test_offline_conformance(server: LoopbackServer, protocol: str, check: str) -> None:
    gateway = _offline_gateway(protocol, server)
    try:
        await CHECKS[check](gateway, "known-model")
    finally:
        await gateway.aclose()


@pytest.mark.parametrize("protocol", PROTOCOLS)
async def test_offline_unknown_model(server: LoopbackServer, protocol: str) -> None:
    server.status = 404
    gateway = _offline_gateway(protocol, server)
    try:
        await rejects_unknown_model_as_not_processed(gateway, "missing-model")
    finally:
        await gateway.aclose()


# --- opt-in live conformance ----------------------------------------------------------------

LIVE = os.environ.get("HOLLIS_CONFORMANCE_BASE_URL")


@pytest.fixture
async def live_gateway() -> AsyncIterator[tuple[ModelGateway, str]]:
    gateway = ModelGateway(live_catalog(dict(os.environ)))
    yield gateway, "live"
    await gateway.aclose()


@pytest.mark.parametrize("protocol", PROTOCOLS)
async def test_live_harness_configuration_against_loopback(
    server: LoopbackServer, protocol: str
) -> None:
    """Exercises the live catalog builder offline, so the opt-in path is known to work."""
    suffix = "/v1" if protocol == "openai-chat-completions" else ""
    catalog = live_catalog(
        {
            "HOLLIS_CONFORMANCE_PROTOCOL": protocol,
            "HOLLIS_CONFORMANCE_BASE_URL": server.base_url + suffix,
            "HOLLIS_CONFORMANCE_MODEL": "known-model",
            "HOLLIS_CONFORMANCE_PROVIDER": "loopback",
        }
    )
    assert [d.locality.value for d in catalog.deployments] == ["local", "local"]
    gateway = ModelGateway(catalog)
    try:
        for check in CHECKS.values():
            await check(gateway, "live")
    finally:
        await gateway.aclose()


live = pytest.mark.skipif(LIVE is None, reason="set HOLLIS_CONFORMANCE_BASE_URL (+ _MODEL) to run")


@live
@pytest.mark.live
@pytest.mark.parametrize("check", CHECKS)
async def test_live_conformance(live_gateway: tuple[ModelGateway, str], check: str) -> None:
    gateway, model = live_gateway
    await CHECKS[check](gateway, model)


@live
@pytest.mark.live
async def test_live_unknown_model(live_gateway: tuple[ModelGateway, str]) -> None:
    gateway, _ = live_gateway
    await rejects_unknown_model_as_not_processed(gateway, "live-missing")
