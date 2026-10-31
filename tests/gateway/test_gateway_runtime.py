"""Runs through the gateway with both protocol adapters over mock transports (no network)."""

from pathlib import Path
from typing import Any

import pytest

from hollis.adapters.config import EndpointConfig
from hollis.adapters.credentials import MissingCredential
from hollis.adapters.memory_store import InMemoryRunStore
from hollis.adapters.sqlite_store import SqliteRunStore
from hollis.gateway import Catalog, ModelGateway, RoutingDenied
from hollis.runtime.events import serialize_event
from hollis.runtime.model import CapabilityMismatch, Locality, UnknownModel
from hollis.runtime.policy import AmbiguousOutcomePolicy, RunBudget
from hollis.runtime.redaction import REDACTED
from hollis.runtime.runner import DeploymentChanged, RunSpec, SecretInRunInput
from hollis.runtime.state import RunStatus
from tests.support import anthropic, openai_chat
from tests.support.http import MockHTTP

ANTHROPIC_KEY = anthropic.TEST_KEY
CORP_TOKEN = "corp-inference-token-0123456789"
ENV = {"ANTHROPIC_API_KEY": ANTHROPIC_KEY, "CORP_TOKEN": CORP_TOKEN}
BUDGET = RunBudget(max_output_tokens=256, max_attempts=2)


def _catalog(
    *,
    allowed: list[str] | None = None,
    local_url: str = "http://127.0.0.1:8000/v1",
    local_caps: dict[str, Any] | None = None,
) -> Catalog:
    data: dict[str, Any] = {
        "deployments": [
            {
                "id": "local-qwen",
                "protocol": "openai-chat-completions",
                "provider": "vllm",
                "model": "Qwen/Qwen3-8B",
                "locality": "local",
                "endpoint": {"base_url": local_url},
                **({"capabilities": local_caps} if local_caps else {}),
            },
            {
                "id": "corp",
                "protocol": "openai-chat-completions",
                "provider": "llama-cpp",
                "model": "llama",
                "locality": "private",
                "endpoint": {
                    "base_url": "https://inference.corp.internal/v1",
                    "auth": {"type": "bearer", "env": "CORP_TOKEN"},
                },
            },
            {
                "id": "claude",
                "protocol": "anthropic-messages",
                "provider": "anthropic",
                "model": "claude-opus-5-5",
                "locality": "external",
                "endpoint": {
                    "base_url": "https://api.anthropic.com",
                    "auth": {"type": "bearer", "env": "ANTHROPIC_API_KEY"},
                },
            },
        ]
    }
    if allowed is not None:
        data["routing"] = {"allowed_localities": allowed}
    return Catalog.from_mapping(data)


class Servers:
    """One mock transport per endpoint origin."""

    def __init__(self, **replies: list[Any]) -> None:
        self.by_origin = {
            "http://127.0.0.1:8000": MockHTTP(*replies.get("local", [])),
            "https://inference.corp.internal": MockHTTP(*replies.get("corp", [])),
            "https://api.anthropic.com": MockHTTP(*replies.get("claude", [])),
        }

    def factory(self, endpoint: EndpointConfig) -> Any:
        return self.by_origin.setdefault(endpoint.origin, MockHTTP()).http_client()

    def __getitem__(self, origin: str) -> MockHTTP:
        return self.by_origin[origin]


def _gateway(
    servers: Servers, catalog: Catalog | None = None, environ: dict[str, str] = ENV
) -> ModelGateway:
    return ModelGateway(catalog or _catalog(), environ=environ, http_client_factory=servers.factory)


@pytest.mark.parametrize(
    ("model", "origin", "reply", "locality"),
    [
        ("local-qwen", "http://127.0.0.1:8000", openai_chat.ok(), Locality.LOCAL),
        ("corp", "https://inference.corp.internal", openai_chat.ok(), Locality.PRIVATE),
        ("claude", "https://api.anthropic.com", anthropic.ok(), Locality.EXTERNAL),
    ],
)
async def test_same_runtime_runs_every_deployment(
    model: str, origin: str, reply: Any, locality: Locality
) -> None:
    servers = Servers(**{"local": [reply], "corp": [reply], "claude": [reply]})
    gateway = _gateway(servers)
    runner = gateway.runner(InMemoryRunStore())

    final = await runner.execute(
        (await runner.create(RunSpec(model=model, input="hi", budget=BUDGET))).run_id
    )

    assert final.status is RunStatus.COMPLETED
    assert (final.deployment.deployment, final.deployment.endpoint, final.deployment.locality) == (
        model,
        origin,
        locality,
    )
    assert final.ambiguous_policy is AmbiguousOutcomePolicy.FAIL  # no endpoint deduplicates
    assert servers[origin].calls == 1
    assert sum(s.calls for s in servers.by_origin.values()) == 1  # nothing went anywhere else
    await gateway.aclose()


async def test_routing_policy_refuses_external_deployments_before_persisting() -> None:
    servers = Servers()
    store = InMemoryRunStore()
    runner = _gateway(servers, _catalog(allowed=["local", "private"])).runner(store)
    with pytest.raises(RoutingDenied):
        await runner.create(RunSpec(model="claude", input="hi", budget=BUDGET))
    assert servers["https://api.anthropic.com"].calls == 0


async def test_unknown_models_and_missing_credentials_fail_clearly() -> None:
    gateway = _gateway(Servers(), environ={})
    with pytest.raises(UnknownModel):
        gateway.resolve("nope")
    with pytest.raises(MissingCredential, match="CORP_TOKEN"):
        gateway.resolve("corp")
    gateway.resolve("local-qwen")  # keyless deployments need no credential


async def test_providers_are_built_once_per_deployment() -> None:
    gateway = _gateway(Servers())
    assert gateway.resolve("local-qwen") is gateway.resolve("local-qwen")


async def test_capability_mismatch_is_rejected_before_persisting() -> None:
    catalog = _catalog(local_caps={"text": {"system_prompt": False, "max_output_tokens": 128}})
    runner = _gateway(Servers(), catalog).runner(InMemoryRunStore())
    with pytest.raises(CapabilityMismatch, match=r"system prompt.*exceeds the limit 128"):
        await runner.create(
            RunSpec(model="local-qwen", input="hi", instructions="be brief", budget=BUDGET)
        )


async def test_resolved_credentials_are_redacted_without_manual_registration(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "runs.db"
    leak = {"error": {"message": f"token {CORP_TOKEN} rejected", "type": "auth"}}
    servers = Servers(corp=[openai_chat.error(401, leak)])
    runner = _gateway(servers).runner(await SqliteRunStore.open(db))

    final = await runner.execute(
        (await runner.create(RunSpec(model="corp", input="hi", budget=BUDGET))).run_id
    )

    assert final.status is RunStatus.FAILED
    assert servers["https://inference.corp.internal"].requests[0].headers["authorization"] == (
        f"Bearer {CORP_TOKEN}"
    )
    events = "".join(
        serialize_event(e) for e in await (await SqliteRunStore.open(db)).read(final.run_id)
    )
    assert CORP_TOKEN not in events
    assert REDACTED in events
    assert CORP_TOKEN.encode() not in db.read_bytes()
    assert all(CORP_TOKEN not in r.getMessage() for r in caplog.records)


async def test_input_containing_a_resolved_credential_is_refused() -> None:
    runner = _gateway(Servers()).runner(InMemoryRunStore())
    with pytest.raises(SecretInRunInput):
        await runner.create(RunSpec(model="corp", input=f"use {CORP_TOKEN}", budget=BUDGET))


async def test_resume_refuses_a_deployment_whose_endpoint_changed() -> None:
    store = InMemoryRunStore()
    run = (
        await _gateway(Servers())
        .runner(store)
        .create(RunSpec(model="local-qwen", input="hi", budget=BUDGET))
    )
    moved = _gateway(Servers(), _catalog(local_url="http://127.0.0.1:9000/v1"))
    with pytest.raises(DeploymentChanged, match="endpoint"):
        await moved.runner(store).execute(run.run_id)
