# Hollis: Vision and Scope

Hollis (working name) is a platform for building, running and orchestrating autonomous agents. It is model-agnostic and local-first, and it's built to be used from Python.

## Positioning

Most agent frameworks fall into one of two camps:

- Thin wrappers around a single vendor's API.
- Large graphs of abstractions where it's hard to tell what an agent did or why.

Hollis bets on the **runtime and harness**: explicit run state, recorded trajectories, recoverable execution and controlled tool access. Agents built on it should be inspectable, resumable and testable. The model is a swappable dependency, not the architecture.

Coding agents are an important specialization, but they sit on a general runtime. Nothing in the core assumes a codebase, a terminal or a specific task type.

## Users

| User | Needs |
|---|---|
| **Agent developer** (primary) | A Python SDK to define agents and tools, run them locally, debug runs and write tests against agent behavior |
| **Application integrator** | Embed agent runs in an existing service through the SDK or REST API, with predictable cost and failure behavior |
| **Operator** | Run agents unattended (locally or in the cloud), inspect history, resume failed runs, enforce tool and resource limits |
| **CLI user** | Start, inspect and resume runs from a terminal without writing code |

## Representative use cases

- A coding agent that reads a repository, edits files and runs tests inside a sandbox.
- A research agent that gathers sources, extracts claims and writes a cited synthesis.
- A data agent that loads a dataset, runs analysis code and reports results.
- A workflow automation: a scheduled agent calls internal APIs and escalates on failure.
- Custom tool-using agents defined by the developer.
- Several agents coordinating on a task, each with scoped tools and context (later phase).

## In scope (eventually)

Python SDK; CLI; REST API; model provider adapters; tool definition and sandboxed execution; persistent run history; context management and memory/retrieval; multi-agent coordination; observability; behavioral evaluation tooling; local execution with a path to cloud deployment.

## Non-goals

- **Virtual workforce / "AI employee" features** (personas, org charts, HR-style management). Deferred indefinitely.
- **Model training or fine-tuning infrastructure.** Hollis consumes models; it doesn't build them.
- **Microservices or distributed infrastructure** in early phases. Hollis is a modular monolith ([ADR-0001](../decisions/0001-modular-monolith-python-uv.md)).
- **A vector database in early phases.** Retrieval comes later and goes behind an adapter.
- **A no-code visual builder.** Code-first is the primary interface.
- **Lock-in to a single model vendor** ([ADR-0002](../decisions/0002-ports-and-adapters.md)).

See [requirements.md](requirements.md) for testable requirements and [roadmap.md](roadmap.md) for sequencing.
