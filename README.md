# Hollis

A model-agnostic, local-first platform for building, running and orchestrating autonomous agents, with a Python SDK first.

**Status: pre-alpha. M4 (context engineering and memory), M3 (tool runtime) and M2 (universal model gateway) are implemented and verified offline; no live provider, inference server or third-party MCP server has been tested yet.** See the [roadmap](docs/product/roadmap.md).

## What exists today

- **Multi-turn tool runtime** ([ADR-0014](docs/decisions/0014-tool-runtime-and-durable-lifecycle.md)). A run alternates model turns and tool calls until it answers, fails, or suspends awaiting approval. One code path handles first execution, retries and resuming after a crash.
  - Every tool call is validated, decided by deterministic policy, optionally approved, then executed and recorded. The event fold refuses execution without authorization.
  - Approvals are bound to the exact tool version and arguments, and survive restarts.
  - Side effects are never blindly repeated: after a crash, only tools declared idempotent are re-run; others are reconciled or reported as `ambiguous`. Exactly-once is not claimed.
  - Limits on model turns, tool calls, parallelism, timeouts and output size; registered secrets are redacted from tool arguments and results; external tool output is framed as untrusted data.
- **Context engine** ([ADR-0018](docs/decisions/0018-context-engine.md)):
  - Every model call is assembled from instructions, the verbatim task, recalled memories, a compaction summary, the recent history and tool specs, within a token budget derived from the deployment's context window.
  - Estimates say whether they're heuristic, calibrated or provider-counted.
  - Long histories are compacted without altering the event log; every action stays listed, and notes are carried forward.
  - Each call's plan is recorded, so retries send identical requests. If the task itself can't fit, the run fails rather than silently dropping it.
- **Large tool results** are stored whole in an artifact store and read on demand, instead of being truncated ([ADR-0019](docs/decisions/0019-artifact-store.md)).
- **Persistent memory** ([ADR-0020](docs/decisions/0020-memory-model-and-lifecycle.md), [ADR-0021](docs/decisions/0021-memory-storage-retrieval-and-deletion.md)):
  - Typed, attributed memories with certainty, validity periods and scopes (`user:alice`, `workspace:acme`, …).
  - Updates, corrections, temporary conditions and conflicts are distinguished, never silently overwritten.
  - Hybrid keyword and metadata recall on SQLite FTS5, at run start and on demand through a tool.
  - Writes are governed by policy: credentials refused, model writes need approval by default. Deletion leaves a tombstone.
  - [Evaluated](docs/architecture/evaluation.md) against no-memory and raw-history baselines.
- **Tools** ([ADR-0015](docs/decisions/0015-tool-definitions-policy-and-approval.md)–[0017](docs/decisions/0017-mcp-integration.md)): `@tool` on a typed function, a registry, rule-based policy, root-scoped filesystem tools, a fixed-command tool, and an MCP client (stdio and Streamable HTTP) with per-tool allowlists and definition pinning.
- **Event-sourced run state.** Every run is an append-only, versioned event log, folded into state. Invalid sequences are rejected loudly; appends use optimistic concurrency.
- **Universal model gateway.** Runs name a logical model. A catalog (Python or `hollis.toml`) maps it to a *deployment*: protocol, endpoint, wire model id, locality and declared capabilities ([ADR-0011](docs/decisions/0011-model-gateway-and-deployments.md)).
- **Two protocol adapters,** each serving many servers ([ADR-0012](docs/decisions/0012-protocol-adapters.md)):
  - `anthropic-messages`, on the official Anthropic SDK: Anthropic's API and Messages-compatible endpoints.
  - `openai-chat-completions`: OpenAI and compatible servers such as vLLM, Ollama, llama.cpp, LM Studio, SGLang, or an enterprise gateway.
  - Their status is in the [compatibility matrix](docs/architecture/model-compatibility.md): *documented*, not yet *verified*.
- **On-prem and local-first** ([ADR-0013](docs/decisions/0013-locality-routing-and-on-prem.md)):
  - Explicit endpoints, keyless local servers, custom CA bundles and mutual TLS, explicit proxies.
  - A routing policy can confine runs to local or private deployments.
  - No silent fallback, no environment-driven routing, no telemetry.
- **Explicit failure handling** ([ADR-0007](docs/decisions/0007-failure-classification-retries-and-ambiguous-policy.md)):
  - Hollis owns all retries.
  - Rejected calls are retried within budget, honouring `retry-after`.
  - Outcomes that may have produced a billed generation are *ambiguous*, and are not retried by default for endpoints that can't deduplicate requests (none of the documented ones can).
- **Budgets:** attempts, output tokens, per-attempt timeout, run deadline.
- **Credentials and redaction** ([ADR-0009](docs/decisions/0009-credentials-and-redaction.md)): deployments reference credentials by environment-variable name. The gateway resolves them and registers them for redaction before any request.
- **Persistence:** in-memory and SQLite stores behind one tested contract.

## Current limitations

- **No live verification.** Adapters are tested against mock transports, documented response shapes and a loopback server. Run the opt-in conformance suite against your server to verify it.
- **No sandbox.** Function and filesystem tools run in-process; the command tool and MCP stdio servers are separate processes with your user's privileges. None of this is a sandbox ([ADR-0016](docs/decisions/0016-execution-isolation.md)); don't use these tools to run untrusted code.
- **Tool calling, compaction and memory are untested against live models,** and the MCP client against third-party servers. The memory evaluation measures whether the right evidence reaches the context; whether a model then answers correctly is tested only by an opt-in live evaluation that hasn't run.
- **Memory retrieval is lexical** (keywords, subjects, entity links); there are no embeddings. Paraphrases with no shared words, and causal links through machine identifiers, can be missed.
- **Deleting a memory doesn't erase copies in run logs** that recalled it. Runs themselves can't be deleted yet.
- **Text generation only; non-streaming.** Responses are capped at about 21k output tokens on the Anthropic protocol.
- **Not yet built:** document knowledge retrieval (RAG, M5); native adapters beyond Anthropic (for example OpenAI's Responses API, Gemini or Bedrock); cancellation and multi-agent orchestration; a CLI or REST API beyond `hollis --version`.
- **No cost accounting or input-token budgets.** Usage is recorded as reported.
- **Run input is stored verbatim.** Secrets embedded in user text are not detected unless they're registered credentials ([details](docs/product/requirements.md#data-persistence-and-secret-handling)).
- **No deletion mechanism yet** (policy only, [ADR-0010](docs/decisions/0010-retention-and-deletion.md)). SQLite data is not encrypted at rest. Single process, single active runner per run.

## Using the gateway

```python
from hollis.adapters.sqlite_store import SqliteRunStore
from hollis.gateway import Catalog, ModelGateway
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import RunSpec

gateway = ModelGateway(Catalog.from_toml("hollis.toml"))  # see hollis.example.toml
runner = gateway.runner(await SqliteRunStore.open("runs.db"))
run = await runner.create(
    RunSpec(model="local-qwen", input="Hello", budget=RunBudget(max_output_tokens=512))
)
final = await runner.execute(run.run_id)
```

## Using tools

```python
from hollis.tools import (
    ApprovalDecision,
    ApprovalRequest,
    CallbackApprover,
    RulePolicy,
    ToolRegistry,
    deny,
    describe,
    tool,
)
from hollis.tools.builtin import filesystem_tools

registry = ToolRegistry()


@registry.add
@tool
async def word_count(text: str) -> int:
    """Count the words in a text."""
    return len(text.split())


registry.extend(filesystem_tools("workspace", writable=True))  # read-only unless writable


def approve(request: ApprovalRequest) -> ApprovalDecision | None:  # return None to suspend the run
    print(describe(request))
    return ApprovalDecision(
        binding=request.binding, approved=input("approve? ") == "y", approver="cli"
    )


runner = gateway.runner(
    store,
    tools=registry,
    policy=RulePolicy.of(deny("fs_*", environments=["prod"], name="no-fs-in-prod")),
    approver=CallbackApprover(approve),  # without an approver, approval-required calls are denied
)
run = await runner.create(
    RunSpec(
        model="claude",
        input="Count the words in notes.txt and save the count",
        tools=tuple(registry.names()),
        budget=RunBudget(max_output_tokens=2048),
    )
)
final = await runner.execute(run.run_id)
```

The default policy runs trusted read-only tools and requires approval for everything else. The deployment must declare tool support (`capabilities.text.tools`).

## Memory and context

```python
from hollis.adapters.artifact_store import SqliteArtifactStore
from hollis.adapters.sqlite_memory import SqliteMemoryStore
from hollis.memory import MemoryAccess, MemoryKind, MemoryManager, memory_tools, user_fact
from hollis.runtime.context import ContextPolicy

memory = MemoryManager(await SqliteMemoryStore.open("memory.db"))
alice = MemoryAccess(read=("user:alice", "workspace:acme"), write=("user:alice",))
await memory.remember(
    user_fact(
        "user:alice",
        "Prefers bullet points",
        kind=MemoryKind.PREFERENCE,
        subject="alice",
        key="format",
    ),
    alice,
)
registry.extend(memory_tools(memory))  # memory_recall (allowed); memory_remember (approval)
runner = gateway.runner(
    store, tools=registry, memory=memory, artifacts=await SqliteArtifactStore.open("artifacts.db")
)
run = await runner.create(
    RunSpec(
        model="claude",
        input="Summarize this week's incidents",
        memory=alice,  # recalled at run start, scoped to these namespaces
        tools=("memory_recall", "memory_remember"),
        context=ContextPolicy(max_history_turns=20),
        budget=RunBudget(max_output_tokens=2048),
    )
)
```

Declare `context_window_tokens` in the deployment's capabilities so the context engine knows the budget.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.12. No API key is needed for development or tests.

```bash
make sync              # install locked dependencies (dev includes all provider extras)
make check             # format check, lint, type check (strict), tests, offline
uv run hollis --version
```

Opt-in verification against real endpoints:

```bash
# Any local or self-hosted OpenAI-compatible server; no API key needed for local servers.
HOLLIS_CONFORMANCE_BASE_URL=http://127.0.0.1:11434/v1 HOLLIS_CONFORMANCE_MODEL=qwen3:8b \
  HOLLIS_CONFORMANCE_PROVIDER=ollama uv run pytest tests/conformance -m live -ra
# Anthropic's API (costs a fraction of a cent).
HOLLIS_LIVE_TESTS=1 ANTHROPIC_API_KEY=... uv run pytest tests/live -m live -ra
```

## Documentation

- [Vision and scope](docs/product/vision.md) · [Requirements](docs/product/requirements.md) · [Roadmap](docs/product/roadmap.md)
- [Architecture overview](docs/architecture/overview.md) · [Model compatibility](docs/architecture/model-compatibility.md) · [Testing strategy](docs/architecture/testing.md) · [Memory evaluation](docs/architecture/evaluation.md)
- [Decision records](docs/decisions/) · Plans: [M1](docs/plans/m1-runtime-core.md), [M2 gateway](docs/plans/m2-gateway.md), [M2 Anthropic](docs/plans/m2-anthropic.md), [M3 tools](docs/plans/m3-tools.md), [M4 context and memory](docs/plans/m4-context-memory.md)
- [Development process](docs/development.md)
- [Research notes](docs/research/)
