# ADR-0001: Modular monolith in Python 3.12+ managed with uv

- Status: Accepted
- Date: 2026-10-31

## Context

Hollis must eventually offer an SDK, CLI and REST API, and run both locally and in the cloud (NFR-8). It's built by a small team with heavy AI assistance. The Python SDK is the primary interface, and most of the model and tool ecosystem is Python-first.

## Decision

- Build Hollis as **one Python package** (`hollis`, src layout), with internal modules and enforced dependency directions.
- Target **Python ≥ 3.12**.
- Use **uv** for environments, dependency groups and the committed lockfile, with `uv_build` as the build backend.
- Gate all changes on `make check`: ruff format, ruff lint, pyright strict and pytest.

## Alternatives considered

- **Microservices from the start**: rejected. They add network failure modes, deployment overhead and cross-service state before any boundary has been proven. That conflicts with "explicit state and predictable behavior".
- **Multiple packages in a workspace** (for example, `hollis-core`, `hollis-providers`): deferred. It's useful once adapters have heavy optional dependencies, and can be done later with optional extras or a uv workspace.
- **Poetry or pip-tools**: uv is faster, handles Python installs, and already bootstraps this repo.

## Trade-offs

- A single process and deploy unit is simpler to reason about, test and debug. Scaling is vertical until a boundary is extracted.
- Module boundaries are only as strong as their enforcement. Without tests, a monolith erodes.

## Consequences

- Import-boundary tests arrive in M1 with the first internal modules, so the boundaries are enforced by tests, not only by convention.
- Heavy adapter dependencies will become optional extras rather than core dependencies.
- Extracting a service later means promoting an existing port to a network boundary, which is possible because of ADR-0002.
