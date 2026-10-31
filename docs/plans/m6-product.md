# M6 plan: developer experience, product interface and brand identity

Branch `m6-product`, from `m5-knowledge`. Owned end to end. No separate approval step.

## Starting point

M0–M5 give a complete *library*: runtime, gateway, tools, context, memory, knowledge. But it has no product surface:
- the CLI prints `--version`;
- there is no server and no persisted notion of an agent, a workspace, a user or a token;
- runs can't be listed or cancelled;
- the only way to use Hollis is to compose Python objects by hand.

## Architecture

```
  Web app (React)      CLI (click)          Python SDK
        |                  |              /          \
        |  HTTPS/JSON+SSE  |  embedded   / embedded   \ remote (HTTP)
        v                  v            v              v
   REST API (FastAPI) ---> hollis.platform: application services <--- one implementation
                           (workspace, agents, runs + supervisor, approvals, deployments,
                            tools, knowledge, memory, artifacts, auth, settings)
                                   |
                 runtime · gateway · tools · memory · knowledge (M0–M5, unchanged semantics)
```

- **`hollis.platform`** is the only place product behaviour lives:
  - a *workspace* (`hollis.toml` plus a `.hollis/` data directory) composes the stores, gateway, tool catalog, policy, memory, knowledge and a run supervisor;
  - every service method takes the calling *principal* and enforces authorization;
  - view models (pydantic) are the wire contract for the API and the remote SDK.
- **REST API** (`hollis.server`, optional extra `server`):
  - FastAPI routers that translate HTTP to service calls, with no domain logic;
  - versioned under `/v1`, OpenAPI generated, errors as RFC 9457 problem details;
  - run events over Server-Sent Events;
  - serves the built web app.
- **Python SDK** (`hollis.Hollis`): one typed API with two backends, embedded (calls the services in-process) and remote (calls the REST API), plus a synchronous facade.
- **CLI** (`hollis`, click): uses the SDK, so it works embedded or against a server (`--server`). Human output by default, `--json` everywhere, meaningful exit codes, shell completion, and an ASCII fallback for the mark.
- **Web app** (`web/`, React + TypeScript + Vite):
  - a bespoke design system: CSS tokens, custom icons, self-hosted IBM Plex;
  - TanStack Query for server state and react-router for routes;
  - built into the Python package so `hollis serve` needs no Node.

## Runtime additions (minimal, evented)

- **Cancellation:** a `RunCancelled` event and a `cancelled` terminal status. Tools that were running when the run was cancelled are named as having an unknown outcome.
- **Run observation:** a runner observer hook called after every durable append. It feeds the event bus (Server-Sent Events) and the run index.
- **Run listing:** `list_runs()` on both stores. The run index in `workspace.db` is a rebuildable cache, not a source of truth.

## Security model

- **Principals** (`admin`, `operator`, `viewer`) hold API tokens. Tokens are stored as hashes and shown once.
- **Server binding:** the server binds to loopback by default and *always* requires authentication. `hollis serve` prints a one-time login link. Browser sessions use HttpOnly SameSite=Strict cookies plus an Origin check.
- **Resource access:**
  - runs are visible to their creator and to admins;
  - memory and knowledge views are filtered by the principal's grants;
  - a run's effective memory and knowledge access is the agent's configuration *intersected* with the caller's grants (no escalation by running a broadly configured agent);
  - approvals require the `approve` permission and access to the run. They go through `Runner.resolve_approval`, so the M3 binding and expiry rules are unchanged.
- **Embedded use** (SDK or CLI without a server) runs as the local workspace administrator. Anyone who can read the data directory already has the data.

## Brand

**Concept: "Continuation".** An open ring (the task: hollow, as in Hollis) joined by a right-angled path to a solid square (the recorded result). It's the shape of a step in a durable run: intent, path, outcome. It echoes the tree and timeline connectors developers read in terminals (`╰─`), which gives the mark a natural ASCII form.

**Palette:** warm ink and paper neutrals with a single brass accent. It's deliberately not purple, blue or a gradient.

**Typography:** IBM Plex Sans and IBM Plex Mono, self-hosted.

**Assets:** generated from one geometric source script:
- SVG icon, mark, wordmark and lockups;
- monochrome, light and dark variants;
- favicon (ICO, PNG, SVG) and application icons.

## Commits

1. This plan.
2. Brand identity: assets and guide.
3. Runtime: cancellation, observer hook, run listing.
4. Platform services, workspace configuration, auth and the deterministic demo model.
5. REST API.
6. Python SDK.
7. CLI.
8. Web application.
9. End-to-end and UI tests.
10. Documentation, ADRs and the final review.
