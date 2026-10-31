# M3 Plan: Tool Runtime and Secure Action Execution

Status: in progress (2026-10-31), owned end to end. Branch `m3-tools`, cut from `m2-anthropic`.

## Sources reviewed

- **MCP:** the official Python SDK `mcp` 2.3.0, read from the wheel (2026-07-28 protocol). The `Client` handles stdio, Streamable HTTP with a caller-supplied `httpx2` client, and in-process servers. Its own docstrings say `ToolAnnotations` hints from untrusted servers must not be relied on.
- **MCP security best practices (2025-11-25):**
  - Local servers: show and require approval of the exact command; minimal privileges; sandbox where possible.
  - SSRF; token passthrough; scope minimization.
- **OWASP Top 10 for Agentic Applications (2026-12-09 announcement):** ASI01 goal hijack, ASI02 tool misuse, ASI03 identity and privilege abuse, ASI04 agentic supply chain, ASI05 unexpected code execution, ASI06 memory and context poisoning, ASI08 cascading failures, ASI09 human–agent trust exploitation.
- **Anthropic Messages tool use:**
  - `tool_use` and `tool_result` blocks.
  - Thinking blocks must be passed back unchanged in tool loops. Thinking is always on for current models.
- **OpenAI Chat Completions:** `tool_calls` with JSON-string arguments; `role: tool` results.

## Design

- **Runtime port (`hollis.runtime.tools`, `conversation`, `results`):** provider-neutral `ToolCall`, `ToolSpec`, `ProviderContinuation`; `ToolDescriptor` (effect, idempotency, reconciliation, trust, isolation, timeout); `Tool` and `ToolCatalog` protocols; `ToolPolicy` with a fail-safe default; the `Approver` protocol; approval binding digests.
- **Durable lifecycle:**
  - Per call: `ToolCallDecided` → `ToolApprovalResolved`? → `ToolExecutionStarted` → `ToolResultRecorded`.
  - The invocation id is `{run}/turn/{n}/call/{i}`.
  - The fold enforces authorization before execution, approval bindings, batch holding during approval, idempotency for re-execution, result consistency, and limits.
- **Bounded loop:**
  - Limits: max model turns, tool calls per run and per turn, parallelism, output size, approval expiry, run deadline.
  - Only all-read-only batches run in parallel; side effects run sequentially in model order.
- **Recovery:** a started invocation with no result is re-executed only if idempotent, reconciled if reconcilable, otherwise recorded as `ambiguous`. Never blindly repeated.
- **Results:** registered secrets redacted; explicit truncation; external results framed as untrusted data at render time.
- **Model integration:** adapters translate history, tool specs, tool calls and results. Anthropic preserves content blocks as a continuation. Capability `tools` is validated before persistence.
- **Developer experience (`hollis.tools`):** a `@tool` decorator deriving one Pydantic model that serves as both the JSON Schema and the validator; a registry; rule-based policy.
- **Ecosystem:**
  - Scoped filesystem tools.
  - A subprocess command tool (isolation `process`, explicitly **not** a sandbox).
  - An MCP client (stdio and Streamable HTTP) with allowlisted tools, operator-declared effects, and definition pinning.

## Weaknesses found and corrected before implementation

1. **Approved calls ran while others in the same turn awaited approval**, which could half-apply a batch. Now nothing in a turn executes until every approval is resolved, enforced in the fold.
2. **Credentials in model-generated arguments would be persisted inside the model response.** Registered secrets are now redacted from responses (including arguments and continuations) before persistence, and such calls are denied by the credential guard.
3. **Parallel execution across mixed effects reorders side effects.** Now only read-only batches run concurrently.
4. **MCP annotations (`readOnlyHint`, …) as authorization input.** Rejected: effects for external tools are declared by the operator, and the default is the most restrictive.
5. **An `ALLOW` recorded before a policy change still stands for that run.** Accepted and documented as a known limitation.

## Out of scope

A verified sandbox backend, declarative HTTP tools, the MCP resources/prompts/sampling/elicitation surfaces, `tools/list_changed` subscriptions, an artifact store for large results, an approval UI, RAG and memory, multi-agent.
