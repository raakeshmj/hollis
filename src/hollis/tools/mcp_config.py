"""MCP server configuration (ADR-0017), free of the MCP SDK so configuration can be loaded and
validated without the optional `mcp` extra. The client lives in `hollis.tools.mcp`."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from hollis.adapters.config import EndpointConfig
from hollis.runtime.tools import ToolEffect


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class McpStdio(_Frozen):
    type: Literal["stdio"] = "stdio"
    command: str
    """Absolute path of the server program. Shown to operators verbatim; no PATH lookup."""
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict[str, str])
    """Extra environment for the server. Nothing else from Hollis's environment is passed
    beyond the SDK's safe defaults (PATH, HOME, USER, ...)."""
    cwd: str | None = None

    @field_validator("command")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("command must be an absolute path")
        return value

    def describe(self) -> str:
        """The exact command line, for operator review."""
        return " ".join([self.command, *self.args])


class McpHttp(_Frozen):
    type: Literal["streamable_http"] = "streamable_http"
    endpoint: EndpointConfig


class McpToolPolicy(_Frozen):
    effect: ToolEffect = ToolEffect.DESTRUCTIVE
    """Operator's declaration. Defaults to the most restrictive effect."""
    idempotent: bool = False
    timeout_s: float = Field(default=30.0, gt=0, le=3600)
    pin: str | None = None
    """Expected definition digest; a mismatch keeps the tool unavailable."""
    description: str | None = None
    """Replaces the server's description (which is untrusted model input)."""


class McpServerConfig(_Frozen):
    name: str = Field(pattern=r"^[a-z0-9_]{1,24}$")
    transport: Annotated[McpStdio | McpHttp, Field(discriminator="type")]
    tools: dict[str, McpToolPolicy] = Field(min_length=1)
    """Allowlist: server tool name -> how Hollis treats it."""
    connect_timeout_s: float = Field(default=30.0, gt=0)
