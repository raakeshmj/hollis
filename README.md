# Hollis

A model-agnostic, local-first platform for building, running and orchestrating autonomous agents, with a Python SDK first.

**Status: pre-alpha. M2 (universal model gateway) is implemented and verified offline; no live provider or inference server has been tested yet.** See the [roadmap](docs/product/roadmap.md).

## What exists today

- **Single-turn runtime.** A run makes one logical model call and then completes or fails. One code path handles first execution, retries and resuming after a crash.
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
- **Text generation only; non-streaming.** Responses are capped at about 21k output tokens on the Anthropic protocol.
- **Not yet built:** native adapters beyond Anthropic (for example OpenAI's Responses API, Gemini or Bedrock); tools, multi-turn runs, memory and multi-agent orchestration; a CLI or REST API beyond `hollis --version`.
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
- [Architecture overview](docs/architecture/overview.md) · [Model compatibility](docs/architecture/model-compatibility.md) · [Testing strategy](docs/architecture/testing.md)
- [Decision records](docs/decisions/) · Plans: [M1](docs/plans/m1-runtime-core.md), [M2 gateway](docs/plans/m2-gateway.md), [M2 Anthropic](docs/plans/m2-anthropic.md)
- [Development process](docs/development.md)
- [Research notes](docs/research/)
