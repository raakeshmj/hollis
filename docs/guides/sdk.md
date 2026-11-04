# Python SDK

One typed API, two ways to reach a workspace:

| | Embedded | Remote |
|---|---|---|
| Open with | `Hollis.open(path)` | `Hollis.connect(url, token)` |
| Acts as | the workspace's local admin | the token's principal |
| Runs execute | in your process (on the client's event loop thread) | in the server |
| Needs | `hollis` | `hollis[client]` and a running `hollis serve` |

Results are the same pydantic models, and errors the same exception types, in both modes. `Hollis.from_env()` connects to `HOLLIS_URL` with `HOLLIS_TOKEN` when they are set, and opens the local workspace otherwise.

## Synchronous

```python
from hollis import Hollis
from hollis.platform.schemas import RunPhase

with Hollis.open("path/to/workspace") as hollis:
    run = hollis.runs.start("summarize docs/release.md", agent="assistant")
    summary = run.wait(timeout_s=300)  # returns when the run finishes or needs a decision
    while summary.phase is RunPhase.WAITING_APPROVAL:
        for approval in run.approvals():
            print(approval.tool, approval.arguments)
        run.approve(reason="checked")  # the only pending call; or approve(invocation_id)
        summary = run.wait()
    print(run.answer())
```

## Asynchronous

```python
import asyncio
from hollis import AsyncHollis


async def main() -> None:
    async with AsyncHollis.connect("http://127.0.0.1:7878", token) as hollis:
        run = await hollis.runs.start("How do we roll back?", agent="assistant")
        async for event in run.stream():  # live; reconnects if the connection drops
            print(event.sequence, event.summary)
        print((await run.detail()).answer)


asyncio.run(main())
```

## What you can do

| Namespace | Operations |
|---|---|
| `runs` | `start(input, agent=…, session=…)` or `start(input, plan=Plan(...))`, `get(id)`, `list(phase=…, agent=…, roots=…)` |
| run handle | `wait`, `refresh`, `detail`, `answer`, `events`, `stream`, `approvals`, `approve`, `deny`, `cancel`, `resume`, `artifact`, `tree` (the orchestration it belongs to), `output` (its checked structured result under a contract) |
| `approvals` | `list`, `approve(approval)`, `deny(approval)` |
| `agents` | `list`, `get`, `create(name=…, model=…, tools=…, delegates=…, policy=…, contract=…, skills=…)`, `update(name, **changes)`, `delete`, `starter`; `create_from_template(template, name, model, parameters=…, **overrides)`, `customize(name, **changes)`, `upgrade_preview(name)`, `upgrade(name, digest=…, reset=…)`, `export_template(name, id=…)` |
| `templates` | `list` (with readiness), `get(id)` (with per-model fit), `capabilities`, `contracts`, `skills` |
| `models` | `list`, `get`, `put(definition)`, `delete`, `probe` (a real request) |
| `tools` | `list` (with the policy's decision), `activity`, `mcp_servers`, `connect_mcp` |
| `knowledge` | `collections`, `create_collection`, `delete_collection`, `sources`, `source`, `ingest`, `ingest_file`, `delete`, `relabel`, `sync`, `search`, `reconcile` |
| `memory` | `list`, `scopes`, `get`, `remember`, `retract`, `forget`, `maintain` |
| `tokens`, `principals` | `list`, `create`, `revoke` (tokens) |
| client | `health`, `settings`, `update_config`, `whoami`, `audit`, `login_token` |

Sessions: runs started with the same `session="…"` share a private memory scope, `session:<you>/<name>`.

Agents that delegate, plans (`from hollis import Plan, PlanStep`) and the orchestration view are described in [orchestration](orchestration.md); templates, contracts, overlays and skills in [specialized agents](specialists.md).

## Errors

All raise subclasses of `hollis.PlatformError` with `code`, `status` and `detail`:

| Exception | Code | Typical cause |
|---|---|---|
| `InvalidRequest` | `invalid_request` | a bad field, an unknown tool or model |
| `Unauthenticated` | `unauthenticated` | missing, expired or revoked token |
| `Forbidden` | `forbidden` | your role or grants don't allow it |
| `NotFound` | `not_found` | it doesn't exist, or isn't visible to you |
| `Conflict` | `conflict` | already exists, or a finished run |
| `Unavailable` | `unavailable` | server unreachable, or a deployment's credential is not set |

## Things to know

- **Embedded runs stop when your process exits.** They stay resumable (`run.resume()`), but nothing drives them in between. Use a server for runs that should outlive your script.
- **Hollis never claims exactly-once execution.** After an interruption, a model call may be retried under its deployment's ambiguity policy, and only tools declared idempotent are re-run; others are reconciled or recorded as ambiguous.
- **The demo model is not a language model.** It exists for trying the platform; point agents at a real deployment for real answers.
- **Lower-level APIs** (the runner, gateway, tools, memory and knowledge used directly) remain available; see the [README](../../README.md).
