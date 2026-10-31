"""Model Context Protocol tools, via the official `mcp` SDK (ADR-0017).

Requires `pip install hollis[mcp]`. Supports the stdio and Streamable HTTP transports.

MCP servers are untrusted until an operator registers them, and registration grants only what the
configuration names:
- Only tools listed in `McpServerConfig.tools` are exposed; everything else the server offers
  is ignored. Tool annotations from the server (readOnlyHint, destructiveHint, ...) are never
  used for authorization: the operator declares each tool's effect, defaulting to DESTRUCTIVE.
- Every exposed tool is `trust=external`: the default policy requires approval, and its results
  are framed as untrusted data for the model. Its description is labelled as coming from the
  server, and an operator can replace it.
- Each tool's definition (name, description, input schema) is digested. An optional pin rejects
  a definition that differs from the one reviewed; a changed definition also changes the tool's
  version, so in-progress runs refuse to resume against it.
- The client advertises no sampling, elicitation or roots capability, so a server cannot ask
  Hollis's model to generate text or the user for input through this connection.
- stdio servers run with an explicit argv (absolute program path, no shell) and only the
  environment the SDK considers safe plus what the configuration lists. They are separate
  processes with the user's privileges: NOT sandboxed.

Not supported in M3: resources, prompts, `tools/list_changed` subscriptions (call `refresh()`),
OAuth flows (use bearer/header auth from the endpoint config).
"""

import contextlib
import hashlib
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaError
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import (
    AudioContent,
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
)
from mcp_types import (
    Tool as McpToolDefinition,
)
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, field_validator

from hollis.adapters._http import build_async_client
from hollis.adapters.config import BearerAuth, EndpointConfig, HeaderAuth
from hollis.adapters.credentials import require_env_secret
from hollis.runtime.redaction import Redactor
from hollis.runtime.tools import (
    Isolation,
    JsonPart,
    ReferencePart,
    ResultPart,
    TextPart,
    ToolArgumentError,
    ToolContext,
    ToolDescriptor,
    ToolEffect,
    ToolError,
    ToolOutput,
    ToolTrust,
    digest,
)

log = logging.getLogger("hollis.tools.mcp")


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


def definition_digest(definition: McpToolDefinition) -> str:
    return digest(
        {
            "name": definition.name,
            "description": definition.description,
            "input_schema": definition.input_schema,
        }
    )


def hollis_name(server: str, tool: str) -> str:
    """mcp_<server>_<tool>, sanitized to the provider-safe pattern and at most 64 characters."""
    name = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp_{server}_{tool}")
    if len(name) <= 64:
        return name
    suffix = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{name[:55]}_{suffix}"


class UnavailableTool(Exception):
    pass


class McpServer:
    """One configured MCP server connection and the tools it is allowed to expose."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        environ: Mapping[str, str] | None = None,
        redactor: Redactor | None = None,
        target: object | None = None,
    ) -> None:
        """`target` overrides the transport (tests pass an in-process server)."""
        self.config = config
        self._environ = environ
        self._redactor = redactor
        self._target = target
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._definitions: dict[str, McpToolDefinition] = {}
        self.tools: dict[str, McpTool] = {}
        self.unavailable: dict[str, str] = {}
        """Allowlisted tools that are not exposed, with the reason."""
        self.ignored: list[str] = []
        """Tools the server offered that are not allowlisted."""

    async def connect(self) -> list["McpTool"]:
        await self.aclose()
        stack = AsyncExitStack()
        try:
            client = Client(
                cast(Any, self._target if self._target is not None else self._transport(stack)),
                read_timeout_seconds=self.config.connect_timeout_s,
                cache=None,
            )
            self._client = await stack.enter_async_context(client)
            definitions = await self._list()
        except BaseException:
            await stack.aclose()
            self._client = None
            raise
        self._stack = stack
        self._register(definitions)
        return list(self.tools.values())

    async def refresh(self) -> None:
        """Re-list tools; definitions that changed since registration become unavailable."""
        if self._client is None:
            await self.connect()
            return
        self._register(await self._list())

    async def call(self, name: str, arguments: dict[str, Any], timeout_s: float) -> CallToolResult:
        if self._client is None:
            await self.connect()  # reconnecting before a request is sent is always safe
        client = self._client
        if client is None or name not in self.tools:
            raise UnavailableTool(name)
        try:
            return await client.call_tool(name, arguments, read_timeout_seconds=timeout_s)
        except MCPError:
            raise
        except BaseException:
            # The request may have been sent; drop the connection and reconnect next time.
            await self.aclose()
            raise

    async def aclose(self) -> None:
        stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

    async def _list(self) -> list[McpToolDefinition]:
        assert self._client is not None  # noqa: S101  # connected by the caller
        definitions: list[McpToolDefinition] = []
        cursor: str | None = None
        for _ in range(100):
            page = await self._client.list_tools(cursor=cursor)
            definitions.extend(page.tools)
            cursor = page.next_cursor
            if cursor is None:
                break
        return definitions

    def _register(self, definitions: list[McpToolDefinition]) -> None:
        offered = {d.name: d for d in definitions}
        self.ignored = sorted(set(offered) - set(self.config.tools))
        self.unavailable = {}
        tools: dict[str, McpTool] = {}
        for name, policy in self.config.tools.items():
            definition = offered.get(name)
            if definition is None:
                self.unavailable[name] = "not offered by the server"
                continue
            found = definition_digest(definition)
            known = self._definitions.get(name)
            if policy.pin is not None and found != policy.pin:
                self.unavailable[name] = (
                    f"definition {found} does not match the pinned {policy.pin}"
                )
                continue
            if known is not None and definition_digest(known) != found:
                self.unavailable[name] = "definition changed since it was registered"
                continue
            try:
                tools[name] = McpTool(self, definition, policy)
            except SchemaError as exc:
                self.unavailable[name] = f"invalid input schema: {exc.message}"
                continue
            self._definitions[name] = definition
        self.tools = tools
        if self.unavailable:
            log.warning("MCP server %s: unavailable tools %s", self.config.name, self.unavailable)

    def _transport(self, stack: AsyncExitStack) -> object:
        transport = self.config.transport
        if isinstance(transport, McpStdio):
            return StdioServerParameters(
                command=transport.command,
                args=list(transport.args),
                env=dict(transport.env),
                cwd=transport.cwd,
            )
        endpoint = transport.endpoint
        client = build_async_client(endpoint)
        stack.push_async_callback(client.aclose)  # the SDK does not close a caller's client
        client.headers.update(endpoint.headers)
        secret = self._credential(endpoint)
        if secret is not None:
            if isinstance(endpoint.auth, BearerAuth):
                client.headers["authorization"] = f"Bearer {secret.get_secret_value()}"
            elif isinstance(endpoint.auth, HeaderAuth):
                client.headers[endpoint.auth.header] = secret.get_secret_value()
        return streamable_http_client(endpoint.base_url, http_client=client)

    def _credential(self, endpoint: EndpointConfig) -> SecretStr | None:
        if not isinstance(endpoint.auth, BearerAuth | HeaderAuth):
            return None
        secret = require_env_secret(endpoint.auth.env, self._environ)
        if self._redactor is not None:
            self._redactor.register(secret)
        return secret


class McpTool:
    """An allowlisted MCP server tool, exposed to Hollis as an external tool."""

    def __init__(
        self, server: McpServer, definition: McpToolDefinition, policy: McpToolPolicy
    ) -> None:
        Draft202012Validator.check_schema(definition.input_schema)
        self._server = server
        self._remote = definition.name
        self._policy = policy
        self._validator = Draft202012Validator(definition.input_schema)
        origin = f"[external tool '{definition.name}' from MCP server '{server.config.name}'] "
        self._descriptor = ToolDescriptor(
            name=hollis_name(server.config.name, definition.name),
            version=definition_digest(definition).removeprefix("sha256:")[:16],
            description=origin + (policy.description or definition.description or ""),
            input_schema=cast(dict[str, JsonValue], definition.input_schema),
            effect=policy.effect,
            idempotent=policy.idempotent,
            trust=ToolTrust.EXTERNAL,
            timeout_s=policy.timeout_s,
            isolation=Isolation.PROCESS,
        )

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._descriptor

    def validate(self, arguments: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        # The stubs leave a legacy overload of iter_errors untyped.
        iter_errors = cast(
            Callable[[object], Iterable[JsonSchemaError]],
            self._validator.iter_errors,  # pyright: ignore[reportUnknownMemberType]
        )
        errors = sorted(iter_errors(dict(arguments)), key=lambda e: e.json_path)
        if errors:
            raise ToolArgumentError(
                "; ".join(f"{e.json_path}: violates '{e.validator}'" for e in errors[:10])
            )
        return dict(arguments)

    async def invoke(self, arguments: dict[str, JsonValue], context: ToolContext) -> ToolOutput:
        try:
            result = await self._server.call(self._remote, arguments, self._policy.timeout_s)
        except UnavailableTool:
            raise ToolError(f"{self._descriptor.name} is currently unavailable") from None
        except MCPError as exc:
            raise ToolError(f"MCP server error {exc.code}: {exc.message[:300]}") from None
        return to_output(result)

    async def reconcile(self, context: ToolContext) -> ToolOutput | None:
        return None


def to_output(result: CallToolResult) -> ToolOutput:
    parts: list[ResultPart] = []
    for block in result.content:
        match block:
            case TextContent(text=text):
                parts.append(TextPart(text=text))
            case ImageContent(mime_type=mime, data=data) | AudioContent(mime_type=mime, data=data):
                parts.append(
                    ReferencePart(
                        kind=block.type,
                        media_type=mime,
                        size_bytes=len(data) * 3 // 4,
                        description="binary content returned by the tool (not inlined)",
                    )
                )
            case ResourceLink():
                parts.append(
                    ReferencePart(
                        kind="resource_link", uri=str(block.uri), media_type=block.mime_type
                    )
                )
            case EmbeddedResource(resource=resource):
                text = getattr(resource, "text", None)
                if isinstance(text, str):
                    parts.append(TextPart(text=text))
                else:
                    parts.append(
                        ReferencePart(
                            kind="resource", uri=str(resource.uri), media_type=resource.mime_type
                        )
                    )
            case _:
                parts.append(
                    ReferencePart(
                        kind=getattr(block, "type", "unknown"), description="unsupported content"
                    )
                )
    if result.structured_content is not None and not any(isinstance(p, TextPart) for p in parts):
        parts.append(JsonPart(data=cast(JsonValue, result.structured_content)))
    return ToolOutput(parts=tuple(parts), is_error=result.is_error)
