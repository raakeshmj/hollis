"""Workspace configuration: hollis init, hollis.toml round trips, discovery."""

import tomllib
from pathlib import Path

import pytest

from hollis.platform.config import (
    ConfigError,
    WorkspaceConfig,
    dumps,
    find_workspace,
    initialize,
    load_config,
    save_config,
)


def test_init_creates_a_safe_default_workspace(tmp_path: Path) -> None:
    assert initialize(tmp_path / "Acme Project")
    root = tmp_path / "Acme Project"
    config = load_config(root)

    assert config.workspace.name == "acme-project"
    assert (config.server.host, config.server.port) == ("127.0.0.1", 7878)
    assert config.demo
    assert config.tools.filesystem is not None
    assert config.tools.filesystem.writable
    assert [r.tools for r in config.policy.rules] == ["knowledge_*"]
    assert (root / ".hollis" / ".gitignore").read_text() == "*\n"
    assert not initialize(root), "an existing workspace is left alone"


def test_configuration_round_trips_through_toml(tmp_path: Path) -> None:
    initialize(tmp_path)
    data = load_config(tmp_path).model_dump(mode="json", exclude_unset=True)
    data["deployments"] = [
        {
            "id": "local-qwen",
            "protocol": "openai-chat-completions",
            "provider": "ollama",
            "model": "qwen3:8b",
            "locality": "local",
            "endpoint": {"base_url": "http://127.0.0.1:11434/v1", "headers": {"x-team": "a b"}},
        }
    ]
    data["tools"]["mcp"] = [
        {
            "name": "files",
            "transport": {
                "type": "stdio",
                "command": "/usr/local/bin/mcp-files",
                "args": ["--root", "."],
            },
            "tools": {"read": {}, "write": {"effect": "mutating"}},
        }
    ]
    data["policy"]["rules"].append(
        {
            "decision": "deny",
            "tools": "mcp_*",
            "environments": ["production"],
            "reason": 'no "quotes"',
        }
    )
    config = WorkspaceConfig.model_validate(data)

    save_config(tmp_path, config)
    reloaded = load_config(tmp_path)

    assert reloaded == config
    assert (tmp_path / "hollis.toml.bak").exists()
    assert (
        tomllib.loads((tmp_path / "hollis.toml").read_text())["deployments"][0]["id"]
        == "local-qwen"
    )


def test_dumps_writes_tables_and_arrays_of_tables() -> None:
    text = dumps({"a": 1, "t": {"s": "x", "u": {"v": True}}, "arr": [{"k": [1, 2]}, {"k": []}]})
    assert tomllib.loads(text) == {
        "a": 1,
        "t": {"s": "x", "u": {"v": True}},
        "arr": [{"k": [1, 2]}, {"k": []}],
    }


def test_invalid_configuration_is_reported_with_the_file(tmp_path: Path) -> None:
    (tmp_path / "hollis.toml").write_text("[server]\nport = 0\n")
    with pytest.raises(ConfigError, match=r"hollis\.toml"):
        load_config(tmp_path)
    (tmp_path / "hollis.toml").write_text("[server\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path)
    with pytest.raises(ConfigError, match=r"no hollis\.toml"):
        load_config(tmp_path / "missing")


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "hollis.toml").write_text("[server]\nhots = '0.0.0.0'\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_workspace_discovery_walks_up_and_honours_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HOLLIS_WORKSPACE", raising=False)
    initialize(tmp_path / "ws")
    nested = tmp_path / "ws" / "src" / "deep"
    nested.mkdir(parents=True)

    assert find_workspace(nested) == (tmp_path / "ws").resolve()
    assert find_workspace(tmp_path) is None
    monkeypatch.setenv("HOLLIS_WORKSPACE", str(tmp_path / "ws"))
    assert find_workspace(tmp_path) == (tmp_path / "ws").resolve()
    monkeypatch.setenv("HOLLIS_WORKSPACE", str(tmp_path))
    assert find_workspace(nested) is None
