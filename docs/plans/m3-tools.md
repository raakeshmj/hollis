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

## Implementation notes (end of M3)

Implemented as designed, with these deviations and findings:

- **Deadline guard added after review.** The first implementation could start a tool after the run deadline had passed, if the deadline expired during approval. `Runner` now fails the run with `budget_exhausted` before executing authorized calls past the deadline (`a57cb3a`, with a test).
- **Deferred approvals are re-presented.** When an approver returns None, each later `execute` pass asks it again for still-pending calls. Approvers must therefore be safe to call repeatedly for the same binding. `resolve_approval` remains the out-of-band path.
- **Synchronous `@tool` functions can't be interrupted.** They run in a worker thread. On timeout the run records `timeout` (read-only) or `ambiguous`, but the thread runs to completion in the background. Prefer async tools for anything slow.
- **MCP `tools/list_changed` isn't subscribed** (ADR-0017). Definition changes are detected on reconnect or `refresh()`.
- **`tests/support/http.py` was renamed to `mock_http.py`.** Run as a module for the stdio MCP test server, it shadowed the stdlib `http` package.
- **Commit grouping:** the runtime loop, tools, adapters, built-in tools, MCP, security tests and docs landed as separate commits. Each passed `make check`.
- **The coding-agent demo from the old roadmap was dropped:** the core must not assume coding tasks, and the end-to-end tests exercise the same loop with neutral tools.
- **Accepted limitation:** a recorded `allow` or approval survives a policy change (weakness 5 above).

Verification: `make check` (ruff, pyright strict, pytest) passes offline. The mutation checks listed in ADR-0014 to ADR-0017 were run by removing each protection and confirming that tests fail. The live tool round trip (`tests/live/test_tool_calling_live.py`) hasn't been run.
