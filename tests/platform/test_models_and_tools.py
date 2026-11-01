"""Models (deployments) and tools through the platform: configuration, status, secrets, policy."""

import pytest

from hollis.platform.auth import Principal
from hollis.platform.errors import Conflict, InvalidRequest, NotFound
from hollis.platform.schemas import AgentInput, DeploymentInput
from hollis.platform.services import Platform
from tests.support.platform import add_agent

ENV = "HOLLIS_TEST_OPENAI_KEY"
SECRET = "sk-test-0123456789abcdef0123456789"


def _remote(deployment_id: str = "gpt") -> DeploymentInput:
    return DeploymentInput(
        deployment={
            "id": deployment_id,
            "protocol": "openai-chat-completions",
            "provider": "openai",
            "model": "gpt-test",
            "locality": "external",
            "endpoint": {
                "base_url": "https://api.openai.example/v1",
                "auth": {"type": "bearer", "env": ENV},
            },
            "capabilities": {"text": {"tools": True, "context_window_tokens": 128000}},
        }
    )


async def test_a_fresh_workspace_offers_the_labelled_demo_model(
    platform: Platform, admin: Principal
) -> None:
    views = {d.id: d for d in await platform.deployments(admin)}
    assert set(views) == {"demo", "local-hashing"}
    assert views["demo"].builtin
    assert "Not a language model" in views["demo"].description
    probe = await platform.probe_deployment(admin, "demo")
    assert probe.ok
    assert probe.detail.startswith("[demo]")


async def test_a_deployment_reports_a_missing_credential_without_exposing_values(
    platform: Platform, admin: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV, raising=False)
    view = await platform.upsert_deployment(admin, _remote())
    assert view.status == "missing_credential"
    assert view.credential is not None
    assert (view.credential.env, view.credential.present) == (ENV, False)
    assert (await platform.health(admin)).status == "degraded"

    monkeypatch.setenv(ENV, SECRET)
    ready = await platform.deployment(admin, "gpt")
    assert ready.status == "ready"
    everything = "".join(
        [
            *(d.model_dump_json() for d in await platform.deployments(admin)),
            (await platform.settings(admin)).model_dump_json(),
            (await platform.health(admin)).model_dump_json(),
            (platform.ws.root / "hollis.toml").read_text(),
        ]
    )
    assert SECRET not in everything
    assert ENV in (platform.ws.root / "hollis.toml").read_text()


async def test_deployments_in_use_cannot_be_deleted(platform: Platform, admin: Principal) -> None:
    await platform.upsert_deployment(admin, _remote())
    await platform.create_agent(admin, AgentInput(name="cloud", model="gpt"))

    with pytest.raises(Conflict):
        await platform.delete_deployment(admin, "gpt")
    await platform.delete_agent(admin, "cloud")
    await platform.delete_deployment(admin, "gpt")
    with pytest.raises(NotFound):
        await platform.deployment(admin, "gpt")


async def test_invalid_and_reserved_deployments_are_rejected(
    platform: Platform, admin: Principal
) -> None:
    with pytest.raises(InvalidRequest):
        await platform.upsert_deployment(admin, DeploymentInput(deployment={"id": "x"}))
    with pytest.raises(Conflict):
        await platform.upsert_deployment(admin, _remote("demo"))
    with pytest.raises(InvalidRequest):
        await platform.create_agent(admin, AgentInput(name="lost", model="no-such-deployment"))
    with pytest.raises(InvalidRequest):
        await platform.create_agent(admin, AgentInput(name="lost", model="demo", tools=("nope",)))


async def test_configuration_changes_survive_a_restart(
    platform: Platform, admin: Principal
) -> None:
    await platform.upsert_deployment(admin, _remote())
    config = platform.ws.config.model_dump(mode="json")
    config["workspace"]["name"] = "renamed"
    await platform.update_config(admin, config)
    root = platform.ws.root
    await platform.close()

    reopened = await Platform.open(root)
    try:
        assert reopened.ws.config.workspace.name == "renamed"
        assert [d.id for d in reopened.ws.config.deployments] == ["gpt"]
        assert (root / "hollis.toml.bak").exists()
    finally:
        await reopened.close()


async def test_invalid_configuration_is_rejected_and_not_saved(
    platform: Platform, admin: Principal
) -> None:
    before = (platform.ws.root / "hollis.toml").read_text()
    with pytest.raises(InvalidRequest):
        await platform.update_config(admin, {"server": {"port": "not a port"}})
    assert (platform.ws.root / "hollis.toml").read_text() == before


async def test_tools_show_their_source_and_the_policy_outcome(
    platform: Platform, admin: Principal
) -> None:
    tools = {t.name: t for t in await platform.tools(admin)}
    write = tools["fs_write_file"]
    assert (write.source, write.policy, write.policy_rule) == (
        "builtin:filesystem",
        "require_approval",
        "default",
    )
    search = tools["knowledge_search"]
    assert (search.policy, search.policy_rule) == ("allow", "knowledge-read")
    assert tools["memory_remember"].policy == "require_approval"
    assert all(t.isolation != "sandbox" for t in tools.values()), "nothing claims a sandbox"


async def test_mcp_servers_are_listed_and_connection_errors_are_shown(
    platform: Platform, admin: Principal
) -> None:
    config = platform.ws.config.model_dump(mode="json")
    config["tools"]["mcp"] = [
        {
            "name": "broken",
            "transport": {"type": "stdio", "command": "/nonexistent/hollis-test-mcp"},
            "tools": {"anything": {}},
        }
    ]
    await platform.update_config(admin, config)

    [server] = await platform.mcp_servers(admin)
    assert (server.name, server.status) == ("broken", "not_connected")
    connected = await platform.connect_mcp(admin, "broken")
    assert connected.status == "error"
    assert connected.error
    with pytest.raises(NotFound):
        await platform.connect_mcp(admin, "missing")


async def test_agents_are_versioned_on_update(platform: Platform, admin: Principal) -> None:
    await add_agent(platform, admin)
    agent = await platform.agent(admin, "helper")
    fields = agent.model_dump(include=set(AgentInput.model_fields))
    spec = AgentInput.model_validate({**fields, "description": "v2"})
    updated = await platform.update_agent(admin, "helper", spec)
    assert (updated.version, updated.description) == (2, "v2")
    assert updated.created_at == agent.created_at
    with pytest.raises(InvalidRequest):
        await platform.update_agent(admin, "helper", spec.model_copy(update={"name": "other"}))
    with pytest.raises(Conflict):
        await platform.create_agent(admin, spec)


async def test_patching_settings_keeps_the_file_minimal(
    platform: Platform, admin: Principal
) -> None:
    await platform.patch_config(admin, {"server": {"run_visibility": "workspace"}})
    assert platform.ws.config.server.run_visibility == "workspace"
    text = (platform.ws.root / "hollis.toml").read_text()
    assert 'run_visibility = "workspace"' in text
    assert "session_hours" not in text, "defaults nobody chose are not written"
    with pytest.raises(InvalidRequest):
        await platform.patch_config(admin, {"server": {"run_visibility": "everyone"}})
