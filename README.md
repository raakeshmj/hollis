# Hollis

A model-agnostic, local-first platform for building, running and supervising autonomous agents: a Python SDK, a CLI, a REST API and a web app.

**Status: pre-alpha. M8 (specialized agents), M7 (multi-agent orchestration), M6 (product interfaces and brand), M5 (knowledge retrieval and RAG), M4 (context engineering and memory), M3 (tool runtime) and M2 (universal model gateway) are implemented and verified offline; no live provider, inference server or third-party MCP server has been tested yet.** See the [roadmap](docs/product/roadmap.md).

## Try it

```bash
uv tool install ".[server]"         # from a checkout: the CLI, REST API and web app
mkdir acme && cd acme && hollis init
hollis run "list the files here"    # the starter agent, on the built-in demo model
hollis serve                        # the web app at http://127.0.0.1:7878 (sign-in link printed)
```

No API key or model download is needed to try it: a fresh workspace has a **demo model**, which is deterministic rules, not a language model (its answers start with `[demo]`), so you can see tools, approvals, memory and knowledge work before connecting a real one. Start with the [quickstart](docs/guides/quickstart.md).

## What exists today

- **Specialized agents** ([ADR-0033](docs/decisions/0033-specialization-templates-and-skills.md)–[0036](docs/decisions/0036-capability-tools-and-protected-paths.md), [guide](docs/guides/specialists.md)). A specialist is an ordinary agent made from a template.
  - **Templates:** seven first-party templates: software engineer, code reviewer, researcher, data analyst, technical writer, automation and coordinator. A template is versioned TOML data: capabilities, a procedure, parameters, requirements and example tasks, and it names no vendor. Agents remember their origin and can be customized, upgraded with a preview, and exported as template files to share.
  - **Capability tools:** code search, outlines and numbered reads; all-or-nothing patches; exact read-only SQL over CSV and JSON files; a Markdown checker; operator-configured commands; read-only git hardened against repository configuration that would run programs. File tools never touch `.git`, `.hollis` or `hollis.toml`, and never write through links.
  - **Output contracts:** a `submit_<contract>` tool whose result is checked against the run's own log. Changed files were changed, checks were run, numbers come from queries, citations were received. Problems go back to the model, and the accepted result is the run's output in every interface and in delegation reports. Accepted means the references were verified, not that the judgement is right.
  - **Policy overlays:** per-agent rules that can only tighten the workspace policy (read-only reviewer, docs-only writer), inherited by everything an agent delegates.
  - **Agent Skills:** `SKILL.md` folders, offered by name and read on demand. Their scripts are never executed.
  - **Evaluation:** [evaluated](docs/architecture/specialist-evaluation.md) against generalist baselines. Specialists cost more tokens, contracts catch detectable errors, and judgement errors pass in every condition.

- **Multi-agent orchestration** ([ADR-0030](docs/decisions/0030-orchestration-on-durable-runs.md)–[0032](docs/decisions/0032-a2a-interoperability.md), [guide](docs/guides/orchestration.md)). It is optional; a single agent stays the default.
  - **Delegation:** agents with `delegates` hand bounded tasks to other agents, each with its own model, tools, memory and knowledge. Every delegated task is a durable child run you can inspect, approve, cancel and resume.
  - **How work runs:** independent read-only tasks run concurrently; dependent work receives earlier results by reference; hierarchies are allowed within a depth limit.
  - **Plans:** a DAG of steps on a deterministic coordinator, with no model and no tokens.
  - **Reports:** built from the child's log. They carry its outcome, its usage, a citation check and verified evidence, and frame the answer as untrusted. Disagreements are kept.
  - **Safety:** permissions only narrow. Depth, fan-out, runs and token budgets are bounded, and cancellation cascades. Recovery never creates a child twice.
  - **Inspection:** a tree in the web app (`hollis runs tree`, `GET /v1/runs/{id}/tree`).
  - **Evaluation:** [evaluated](docs/architecture/orchestration-evaluation.md) against single-agent baselines, including where multiple agents lose.

- **Product interfaces** ([ADR-0026](docs/decisions/0026-workspace-and-platform-layer.md)–[0029](docs/decisions/0029-web-application-and-design-system.md)), all over one service layer, so they behave identically:
  - **Workspace:** `hollis.toml` (configuration, safe to commit) and `.hollis/` (SQLite stores). `hollis init` creates one with a starter agent.
  - **CLI** (`hollis`): run agents and follow them live, decide approvals at the terminal, manage agents, models, tools, knowledge, memory, tokens and principals; JSON output, documented exit codes, shell completion ([reference](docs/reference/cli.md)).
  - **REST API** (`hollis serve`): versioned under `/v1`, OpenAPI, RFC 9457 errors, Server-Sent Events and long polling for runs that take minutes or wait days for an approval ([reference](docs/reference/rest-api.md)).
  - **Python SDK:** `Hollis` and `AsyncHollis`, the same typed API embedded in your process or against a server ([guide](docs/guides/sdk.md)).
  - **Web app:** follow runs live on a timeline, approve or deny tool calls, manage everything above; light and dark themes, keyboard accessible, responsive ([guide](docs/guides/web.md)).
  - **Security:** authentication always required, loopback by default, hashed tokens, single-use sign-in links, strict cookies, roles and grants enforced in the services; a run's access is the agent's intersected with its caller's.
  - **Reliability:** runs execute in the background, survive dropped connections and restarts, can be cancelled, and resume from their event logs.
  - **Brand:** the Hollis identity and its guide are in [`brand/`](brand/README.md).

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
- **Knowledge retrieval and RAG** ([ADR-0022](docs/decisions/0022-embedding-task.md)–[0025](docs/decisions/0025-rag-grounding-and-security.md)):
  - **Ingestion:** Markdown, HTML, code, JSON, CSV, DOCX and PDF (optional extra), with structure, locations, versions and content fingerprints. Directory sync removes deleted files; unchanged content is never re-indexed.
  - **Retrieval:** hybrid lexical, dense and exact-identifier retrieval on SQLite, with access labels enforced inside every query. Embeddings come from any OpenAI-compatible endpoint through the gateway, or from a local model-free hashing embedder.
  - **In runs:** cited passages in context (kept separate from memory), plus search, outline, read and find tools for large documents.
  - **Grounding:** a report flags fabricated or unsupported citations.
  - **Evaluation:** compared across configurations on a development and a held-out corpus ([evaluation](docs/architecture/rag-evaluation.md)).
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
- **No real embedding model has been tested.** Dense retrieval is evaluated only with the local hashing embedder (surface similarity, not meaning) and a simulated one. Memory recall remains lexical. The grounding check is lexical: it verifies citations, not truth, and doesn't detect negation.
- **Exact dense scan** is linear (about 240 ms at 20,000 chunks of 384 dimensions); larger collections need an approximate index. There's no OCR, multimodal retrieval or web connector.
- **Deleting a memory doesn't erase copies in run logs** that recalled it. Runs themselves can't be deleted yet.
- **Text generation only; non-streaming.** Responses are capped at about 21k output tokens on the Anthropic protocol.
- **Not yet built:** native adapters beyond Anthropic (for example OpenAI's Responses API, Gemini or Bedrock); A2A interoperability with other vendors' agents ([ADR-0032](docs/decisions/0032-a2a-interoperability.md)).
- **Specialists and orchestration are untested with live models.** Both evaluations use deterministic simulated agents; whether a template's procedure changes a real model's behaviour is measured only by an opt-in live evaluation that hasn't run. Configured commands and git run with your user's privileges (process isolation, not a sandbox), and git tools trust the repository's own `.git/config`.
- **Orchestration:** real coordinators write vaguer assignments and misroute more; expect worse results than the offline numbers.
- **Interfaces:** no TLS termination or external identity provider (put a proxy in front for network use; [deployment](docs/guides/deployment.md)); one server process per workspace; the web app is tested in Chrome only, and accessibility is checked automatically, not with screen-reader users. Embedded SDK runs stop when your process exits (they stay resumable).
- **No cost accounting or input-token budgets.** Usage is recorded as reported.
- **Run input is stored verbatim.** Secrets embedded in user text are not detected unless they're registered credentials ([details](docs/product/requirements.md#data-persistence-and-secret-handling)).
- **No deletion mechanism yet** (policy only, [ADR-0010](docs/decisions/0010-retention-and-deletion.md)). SQLite data is not encrypted at rest. Single process, single active runner per run.

## Lower-level APIs

The platform composes these from `hollis.toml`; you can also use them directly.

### Using the gateway

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

### Using tools

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

### Memory and context

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

### Knowledge retrieval

```python
from hollis.adapters.sqlite_knowledge import SqliteKnowledgeStore
from hollis.knowledge.ingest import KnowledgeBase
from hollis.knowledge.retrieve import KnowledgeRetriever
from hollis.knowledge.tools import knowledge_tools
from hollis.runtime.knowledge import KnowledgeAccess

embedder = gateway.embedder("local-embed")  # or HashingEmbedder(): no model needed
store = await SqliteKnowledgeStore.open("knowledge.db")
kb = KnowledgeBase(store, embedder=embedder, redactor=gateway.redactor)
await kb.create_collection("runbooks")
await kb.sync_directory("runbooks", "docs/runbooks", labels=("group:sre",))
retriever = KnowledgeRetriever(store, embedder=embedder)
registry.extend(knowledge_tools(store, retriever))  # search, list, outline, read, find
runner = gateway.runner(run_store, tools=registry, knowledge=retriever)
run = await runner.create(
    RunSpec(
        model="claude",
        input="The ETL job failed with CUDA_ERROR_OUT_OF_MEMORY. What now?",
        knowledge=KnowledgeAccess(collections=("runbooks",), labels=("group:sre",)),
        tools=("knowledge_search", "knowledge_read"),
        budget=RunBudget(max_output_tokens=2048),
    )
)
final = await runner.execute(run.run_id)
final.grounding  # per-claim support for the answer's citations
```

## Development

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.12. No API key is needed for development or tests. Changing the web app also needs Node 22.12+ ([development](docs/development.md)).

```bash
make sync              # install locked dependencies (dev includes all extras)
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

- [Installation](docs/guides/installation.md) · [Quickstart](docs/guides/quickstart.md) · [SDK](docs/guides/sdk.md) · [Web app](docs/guides/web.md) · [Specialized agents](docs/guides/specialists.md) · [Multi-agent orchestration](docs/guides/orchestration.md) · [Running a shared server](docs/guides/deployment.md) · [Examples](examples/)
- Reference: [CLI](docs/reference/cli.md) · [REST API](docs/reference/rest-api.md) · [Configuration](docs/reference/configuration.md) · [Brand guide](brand/README.md)
- [Vision and scope](docs/product/vision.md) · [Requirements](docs/product/requirements.md) · [Roadmap](docs/product/roadmap.md)
- [Architecture overview](docs/architecture/overview.md) · [Model compatibility](docs/architecture/model-compatibility.md) · [Testing strategy](docs/architecture/testing.md) · [Memory evaluation](docs/architecture/evaluation.md) · [RAG evaluation](docs/architecture/rag-evaluation.md) · [Orchestration evaluation](docs/architecture/orchestration-evaluation.md) · [Specialist evaluation](docs/architecture/specialist-evaluation.md)
- [Decision records](docs/decisions/) · Plans: [M1](docs/plans/m1-runtime-core.md), [M2 gateway](docs/plans/m2-gateway.md), [M2 Anthropic](docs/plans/m2-anthropic.md), [M3 tools](docs/plans/m3-tools.md), [M4 context and memory](docs/plans/m4-context-memory.md), [M5 knowledge](docs/plans/m5-knowledge.md), [M6 product](docs/plans/m6-product.md), [M7 orchestration](docs/plans/m7-orchestration.md), [M8 specialists](docs/plans/m8-specialized-agents.md)
- [Development process](docs/development.md)
- [Research notes](docs/research/)
