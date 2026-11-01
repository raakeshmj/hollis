"""Workspace configuration: `hollis.toml` (ADR-0026).

A workspace is a directory with a `hollis.toml` (configuration, safe to commit: it references
credentials by environment-variable name only) and a `.hollis/` data directory (databases; not
for version control). `hollis init` creates both.

    [workspace]
    name = "acme"

    [server]
    host = "127.0.0.1"          # loopback by default; authentication is always required
    port = 7878

    [runtime]
    environment = "local"       # label used by tool policy rules

    [[deployments]]             # same schema as the gateway catalog (ADR-0011)
    id = "local-qwen"
    protocol = "openai-chat-completions"
    ...

    [tools.filesystem]          # optional: root-scoped file tools
    root = "."
    writable = false

    [[policy.rules]]            # deterministic tool policy (ADR-0015)
    decision = "allow"
    tools = "knowledge_*"

    [orchestration]             # limits on agents delegating to agents (ADR-0031)
    max_depth = 2
    token_budget = 200000
"""

import os
import re
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from hollis.gateway.catalog import Deployment, RoutingPolicy
from hollis.runtime.delegation import DelegationLimits
from hollis.runtime.tools import Decision, ToolEffect, ToolTrust
from hollis.tools.mcp_config import McpServerConfig

CONFIG_FILE = "hollis.toml"
DATA_DIR = ".hollis"
NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{0,62}$"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WorkspaceSection(_Model):
    name: str = Field(default="hollis", min_length=1, max_length=80)


class ServerConfig(_Model):
    host: str = "127.0.0.1"
    port: int = Field(default=7878, ge=1, le=65535)
    session_hours: float = Field(default=12.0, gt=0, le=24 * 30)
    run_visibility: Literal["owner", "workspace"] = "owner"
    """Who can see a run: its creator and admins ("owner"), or every principal ("workspace")."""
    allowed_hosts: tuple[str, ...] = ()
    """Host names accepted besides loopback and the bind address (e.g. behind a proxy). "*"
    accepts any Host header; use it only when a proxy in front validates it."""


class RuntimeConfig(_Model):
    environment: str = Field(default="local", min_length=1, max_length=40)
    max_output_tokens: int = Field(default=1024, ge=1)
    """Default output budget for agents that do not set one."""
    default_agent: str | None = None
    """The agent a run uses when the request names neither an agent nor a model."""


class FilesystemTools(_Model):
    root: str = "."
    """Relative to the workspace directory."""
    writable: bool = False


class ToolsConfig(_Model):
    filesystem: FilesystemTools | None = None
    mcp: tuple[McpServerConfig, ...] = ()
    memory_forget: bool = False
    """Offer the destructive memory_forget tool to agents."""


class PolicyRuleConfig(_Model):
    decision: Literal[Decision.ALLOW, Decision.REQUIRE_APPROVAL, Decision.DENY]
    tools: str = "*"
    effects: tuple[ToolEffect, ...] = ()
    trust: tuple[ToolTrust, ...] = ()
    environments: tuple[str, ...] = ()
    delegated: bool | None = None
    """true: only runs another agent delegated; false: only runs nobody delegated."""
    name: str = ""
    reason: str = ""


class PolicyConfig(_Model):
    rules: tuple[PolicyRuleConfig, ...] = ()


class KnowledgeConfig(_Model):
    embedder: str = "local-hashing"
    """`local-hashing` (built in, no model) or the id of an `openai-embeddings` deployment."""
    sync_roots: tuple[str, ...] = (".",)
    """Directories (relative to the workspace) the API may sync from."""


class OrchestrationConfig(DelegationLimits):
    """Workspace-wide delegation limits (ADR-0031). An agent's own `delegation` limits can only
    narrow them."""


class WorkspaceConfig(_Model):
    workspace: WorkspaceSection = WorkspaceSection()
    server: ServerConfig = ServerConfig()
    runtime: RuntimeConfig = RuntimeConfig()
    demo: bool = True
    """Offer the built-in deterministic demo model as deployment `demo`."""
    deployments: tuple[Deployment, ...] = ()
    routing: RoutingPolicy = RoutingPolicy()
    tools: ToolsConfig = ToolsConfig()
    policy: PolicyConfig = PolicyConfig()
    knowledge: KnowledgeConfig = KnowledgeConfig()
    orchestration: OrchestrationConfig = OrchestrationConfig()


class ConfigError(ValueError):
    pass


def find_workspace(start: Path | None = None) -> Path | None:
    """The nearest directory at or above `start` containing hollis.toml (HOLLIS_WORKSPACE wins)."""
    env = os.environ.get("HOLLIS_WORKSPACE")
    if env:
        path = Path(env).expanduser().resolve()
        return path if (path / CONFIG_FILE).is_file() else None
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        if (directory / CONFIG_FILE).is_file():
            return directory
    return None


def load_config(root: Path) -> WorkspaceConfig:
    path = root / CONFIG_FILE
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"no {CONFIG_FILE} in {root}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    try:
        return WorkspaceConfig.model_validate(data)
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def save_config(root: Path, config: WorkspaceConfig) -> None:
    """Write hollis.toml atomically, keeping the previous file as hollis.toml.bak. Comments in a
    hand-edited file are not preserved; the backup keeps them."""
    path = root / CONFIG_FILE
    data = config.model_dump(mode="json", exclude_unset=True)
    text = "# Hollis workspace configuration. See docs/reference/configuration.md.\n\n" + dumps(
        data
    )
    if path.exists():
        (root / f"{CONFIG_FILE}.bak").write_text(path.read_text())
    temporary = path.with_suffix(".toml.tmp")
    temporary.write_text(text)
    temporary.replace(path)


INITIAL_CONFIG = """# Hollis workspace configuration. See docs/reference/configuration.md.
# Safe to commit: credentials are referenced by environment-variable name, never stored here.

[workspace]
name = "{name}"

[server]
host = "127.0.0.1"   # loopback only; authentication is always required
port = 7878

[runtime]
environment = "local"
default_agent = "assistant"   # `hollis init` creates it, on the demo model

# The built-in demo model (deployment "demo") is deterministic rules, not a language model.
# Add real deployments with `hollis models add` or the web app, for example:
#
# [[deployments]]
# id = "local-qwen"
# protocol = "openai-chat-completions"
# provider = "ollama"
# model = "qwen3:8b"
# locality = "local"
# endpoint = {{ base_url = "http://127.0.0.1:11434/v1" }}

[tools.filesystem]
root = "."
writable = true      # writes always need approval under the default policy

[[policy.rules]]
decision = "allow"
tools = "knowledge_*"
name = "knowledge-read"
reason = "read-only knowledge tools"
"""


def initialize(root: Path, name: str | None = None) -> bool:
    """Create hollis.toml and .hollis/ if missing. Returns False if a workspace already exists."""
    root.mkdir(parents=True, exist_ok=True)
    (root / DATA_DIR).mkdir(exist_ok=True)
    gitignore = root / DATA_DIR / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n")
    path = root / CONFIG_FILE
    if path.exists():
        return False
    label = name or re.sub(r"[^a-z0-9-]+", "-", root.resolve().name.lower()).strip("-") or "hollis"
    path.write_text(INITIAL_CONFIG.format(name=label))
    return True


# --- minimal TOML writer -----------------------------------------------------------------------


def _key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else _string(key)


def _string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_value(v) for v in cast(list[Any], value)) + "]"
    if isinstance(value, dict):
        items = cast(dict[str, Any], value)
        return (
            "{ "
            + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in items.items() if v is not None)
            + " }"
        )
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def dumps(data: dict[str, Any], path: str = "") -> str:
    """TOML for nested dicts and lists: each table's scalars, then sub-tables, then arrays of
    tables. Dicts inside array entries are written as inline tables, which TOML allows."""
    scalars: list[str] = []
    tables: list[tuple[str, dict[str, Any]]] = []
    arrays: list[tuple[str, list[dict[str, Any]]]] = []
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, dict):
            tables.append((key, cast(dict[str, Any], value)))
        elif (
            isinstance(value, list)
            and value
            and all(isinstance(item, dict) for item in cast(list[Any], value))
        ):
            arrays.append((key, cast(list[dict[str, Any]], value)))
        else:
            scalars.append(f"{_key(key)} = {_value(value)}")
    out = ""
    if scalars:
        out += (f"\n[{path}]\n" if path else "") + "\n".join(scalars) + "\n"
    for key, table in tables:
        out += dumps(table, f"{path}.{_key(key)}" if path else _key(key))
    for key, items in arrays:
        name = f"{path}.{_key(key)}" if path else _key(key)
        for item in items:
            body = "".join(f"{_key(k)} = {_value(v)}\n" for k, v in item.items() if v is not None)
            out += f"\n[[{name}]]\n{body}"
    return out
