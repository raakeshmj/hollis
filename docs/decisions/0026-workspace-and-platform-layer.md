# ADR-0026: Workspace and one platform layer for every interface

- Status: Accepted (M6)
- Date: 2026-11-01

## Context

M1–M5 built a library: a runner, a gateway, tools, memory and knowledge, each composed by hand in user code. M6 adds a CLI, a REST API, an SDK with embedded and remote modes, and a web app (FR-10, FR-11, FR-17). If each interface composed the pieces itself, run semantics, authorization and defaults would drift between them.

## Decision

**A workspace** is a directory with `hollis.toml` and `.hollis/`:
- `hollis.toml` holds configuration: deployments (the gateway catalog), routing, tools (filesystem, MCP), policy rules, knowledge settings, server settings. It is safe to commit: credentials are environment-variable names. Saves are atomic and keep `hollis.toml.bak`.
- `.hollis/` holds `workspace.db` (principals, token and session hashes, agents, run index, append-only audit log), `runs.db`, `memory.db`, `knowledge.db` and `artifacts.db`, and a `.gitignore`.

**`hollis.platform` is the only application layer.** `Workspace` composes the M1–M5 parts from the configuration (gateway, demo model, embedder, tool registry with sources, rule policy, memory manager, knowledge base and retriever, one `Runner` with deferred approvals). `Platform` services take the calling principal on every method and return view models computed from the runtime's own state and events. The REST API, the SDK's local backend and, through the SDK, the CLI call these methods and nothing else. No interface re-derives run semantics.

**Runs execute in the background.** `RunSupervisor` drives each run as an asyncio task in the process that owns the workspace; starting a run returns at once. `EventBus` (a runner observer) wakes waiters and streams. Shutdown cancels tasks and leaves runs resumable; `recover()` resumes interrupted runs (never those waiting for approval) at server start.

**The run index** in `workspace.db` pairs ownership recorded at start (agent, creator, session) with columns derived from the event log, which `sync_index` can re-derive. The event logs stay the source of truth.

**One process owns the stores** (ADR-0004 is unchanged). While `hollis serve` runs, it writes an owner-only marker `.hollis/server.json` (URL and a token revoked at shutdown); CLI commands in that workspace detect it and go through the server instead of opening the stores.

**A built-in demo model** (`demo`, protocol `hollis-demo`) is deterministic rules, not a language model. Every reply starts with "[demo]", the UI and CLI label it wherever it appears, and it can be turned off (`demo = false`). It lets a fresh workspace exercise tools, approvals, memory and knowledge without credentials. `hollis init` creates a starter agent (`assistant`) and a `docs` collection, and sets `runtime.default_agent`.

## Alternatives considered

- **Interfaces composing the library directly:** duplicated logic and divergent authorization.
- **A daemon always running:** heavier for local use; embedded mode serves scripts and tests.
- **Configuration in the database:** not reviewable or committable; the TOML file is.

## Trade-offs

One application layer is a large module, but it is the single place to read for behaviour. The demo model risks being mistaken for a model; labelling everywhere and the `[demo]` prefix address that.

## Consequences

Enforced by tests: platform journeys (`tests/platform/`), authorization (mutation-checked), import boundaries (the platform, SDK and CLI load no optional dependency), recovery after restart and after shutdown mid-call.
