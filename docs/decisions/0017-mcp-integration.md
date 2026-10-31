# ADR-0017: Model Context Protocol integration

- Status: Accepted (M3)
- Date: 2026-10-31

## Context

MCP is the main interoperability standard for external tools. The official Python SDK, `mcp` 2.3.0 (2026-07-28 protocol, read from the wheel), provides a `Client` over stdio, Streamable HTTP (with a caller-supplied `httpx2` client) and in-process servers. Its own `ToolAnnotations` docstring warns that hints from untrusted servers must not be relied on. The MCP security best practices cover malicious local servers (show the exact command, run with least privilege), SSRF, token passthrough and scope minimization. OWASP ASI04 covers poisoned agentic supply chains, including "rug-pull" tool definitions.

## Decision

`hollis.tools.mcp` (optional extra `hollis[mcp]`), using the official SDK, with no custom protocol code. Connecting to a server grants nothing by itself.

**Registration:**
- `McpServerConfig.tools` is an allowlist. Unlisted server tools are ignored and reported.
- Each listed tool gets an operator-declared `effect` (default `destructive`), `idempotent` flag, timeout, optional definition **pin**, and optional replacement description.
- Server annotations are never used for authorization.

**Exposure:**
- Tools are `trust=external`, so the default policy requires approval and results are framed as untrusted data.
- Name: `mcp_<server>_<tool>`, provider-safe, at most 64 characters, with a hash suffix when long.
- Descriptions are labelled with their origin and capped at 2,000 characters.
- Version: a digest of the server's definition, so a changed definition changes the tool's identity and in-progress runs refuse to resume.

**Rug-pull defence:**
- A pin rejects any definition other than the reviewed one.
- A definition that changes after registration (detected on reconnect or `refresh()`) makes the tool unavailable.

**Validation and results:**
- Arguments are validated with the server's schema (`jsonschema` Draft 2020-12, schema-checked at registration).
- Results map to text, JSON and reference parts; images, audio and binaries are described, not inlined.
- Server errors become tool errors. Connection loss after sending propagates, so it's `error` for read-only tools and `ambiguous` otherwise, and the client reconnects (before sending) on the next call.

**No reverse capabilities:** no sampling, elicitation or roots callbacks are registered, so a server cannot make Hollis's model generate text or ask the user for input through this connection.

**Transports:**
- **stdio:** absolute program path, explicit args, the SDK's safe environment subset plus listed variables, optional cwd. The exact command is available via `McpStdio.describe()` for operator review.
- **Streamable HTTP:** the M2 `EndpointConfig` (auth by environment reference, TLS, explicit proxy, no proxy environment). Credentials are registered for redaction. The SDK refuses cross-origin redirects. The HTTP client is closed with the connection.

## Alternatives considered

- **A custom MCP client:** duplicates a maintained SDK.
- **Trusting annotations or auto-exposing all tools:** that's the supply-chain attack surface.
- **`tools/list_changed` subscriptions:** on 2026-07-28 sessions they require `subscriptions/listen`. Deferred; `refresh()` and reconnect cover detection for now.
- **OAuth flows:** deferred. Bearer and header auth come from the endpoint config.

## Trade-offs

- Operators must curate allowlists and declare effects.
- Without a subscription, a definition change is noticed only on reconnect or `refresh()`. In the meantime the server-side behaviour may already differ from the reviewed definition.
- stdio servers are unsandboxed processes with the user's privileges (ADR-0016).

## Consequences

- **Evidence** (`tests/tools/test_mcp.py`):
  - In-process server: allowlist, lying annotations ignored, schema validation, pins, changed-definition lockout, description cap, a full run with approval and framing.
  - A real stdio subprocess with a scrubbed environment.
  - Streamable HTTP over loopback with bearer auth enforced by the server.
- **Live interoperability with third-party MCP servers has not been tested.**
