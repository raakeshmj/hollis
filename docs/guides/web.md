# The web app

`hollis serve` serves the web app and the REST API from one address, by default `http://127.0.0.1:7878`. Everything the app shows comes from the API; there is no demo data. When runs use the built-in demo model, the app says so.

## Signing in

- **The link `hollis serve` prints** works once, within ten minutes. The token is in the part of the URL after `#`, which browsers never send to a server; the app removes it from the address bar as soon as it reads it.
- **A new link** for the running server: `hollis login-link` (add `--open` to open it).
- **An API token** (`hollis tokens create`) pasted on the sign-in page.

A browser session lasts `server.session_hours` (12 by default). Sign out ends it on the server.

## What's where

| Section | For |
|---|---|
| Overview | health, what is waiting for you, starting a run, recent runs |
| Runs | every run you can see, filtered by phase and agent (delegated runs hidden unless you include them); starting runs |
| Run inspector | a live timeline: model calls (attempts, stop reasons, usage), tool calls (policy decision, approval, arguments, results, artifacts), the checked output under the agent's contract (accepted, rejected with its problems, or missing), the answer with citations, grounding, passages and memories used, the run's access and context budget, and the raw event log. For runs that delegated or were delegated: the orchestration (every agent's run, objective, outcome, citation check, usage, what it waits on and was not granted, a timeline of parallel work, refusals and disagreements), the lineage and the assignment ([orchestration](orchestration.md)) |
| Approvals | tool calls held for a person, with the exact arguments and, for delegated runs, which agents delegated them; approve or deny with a recorded reason |
| Agents | definitions: model, instructions, tools (with what the policy decides for each), memory and knowledge access, budget, delegates and delegation limits, output contract, offered skills and policy overlay rules; for agents made from a template, its version, parameters, customized fields, the upgrade it offers (what changes and what is kept) and export as a template file |
| Templates | the gallery of first-party and workspace templates: what each produces, what it is built from and whether this workspace can run it; a template's procedure, requirements, per-model fit and contract rules, and a form to create an agent from it ([specialized agents](specialists.md)) |
| Tools | every tool, its effect, trust and policy outcome; MCP servers and their status; recent tool calls |
| Models | deployments, whether each is ready (credential variable set, allowed by routing), adding, testing and removing them |
| Knowledge | collections, uploading documents with access labels, document structure and chunks, searching as an agent would |
| Memory | memories by scope, kind and status, where each came from, its history; remembering, retracting, forgetting |
| Settings | the workspace, your access, API tokens, configuration, people and services, the audit log |

The app shows controls only where your role allows them; the server checks every action regardless. Approving needs the approve permission and access to the run; your name is recorded with every decision.

## Themes, keyboard and screen readers

The app follows your system's light or dark setting; the sidebar's theme button cycles system, light and dark (stored in this browser). It works with the keyboard alone (start with **Tab** for "Skip to content"), labels status with words as well as colors, and respects reduced motion.

## Developing the app

The source is in `web/` (React, TypeScript, Vite). With a server running:

```bash
make web-install
cd web && npm run dev        # http://localhost:5173, proxying /v1 to 127.0.0.1:7878
```

`make web-check` runs the type check, ESLint (with strict accessibility rules) and component tests; `make web-build` writes the build into `src/hollis/server/static` (committed). After changing the API, run `make web-api` to regenerate `web/openapi.json` and the TypeScript types; a test fails while they are out of date. `make web-e2e` runs the browser journeys in your installed Google Chrome against a real server.
