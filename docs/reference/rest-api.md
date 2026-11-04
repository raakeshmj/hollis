# REST API reference

Base path `/v1`. The machine-readable contract is the OpenAPI document at `GET /v1/openapi.json` (also committed as `web/openapi.json`). The [SDK](../guides/sdk.md) wraps every operation; this page is for other languages and for understanding the wire format. Design: [ADR-0027](../decisions/0027-rest-api-contract.md); security: [ADR-0028](../decisions/0028-authentication-and-authorization.md).

## Authentication

Every route except `GET /v1/ping` requires one of:

- `Authorization: Bearer hx_…`, an API token (`hollis tokens create`, `POST /v1/tokens`).
- The `hollis_session` cookie from `POST /v1/session` (browsers). Requests that change anything must also send an `Origin` header naming this server; no CORS headers are sent, so other sites can't read responses.

Requests whose `Host` header is not loopback, the bind address or a configured allowed host are refused with 400.

## Errors

Failures are [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) problem details, `Content-Type: application/problem+json`:

```json
{"type": "urn:hollis:problem:not_found", "title": "Not found", "status": 404,
 "detail": "no run 9f1c…", "code": "not_found"}
```

| Status | `code` | |
|---|---|---|
| 400 | `bad_host` | unexpected `Host` header |
| 401 | `unauthenticated` | missing, invalid, expired or revoked credentials (`WWW-Authenticate: Bearer`) |
| 403 | `forbidden` | your role or grants don't allow it, or a cross-origin cookie request |
| 404 | `not_found` | doesn't exist, **or isn't visible to you**: identifiers alone reveal nothing |
| 405 | `method_not_allowed` | |
| 409 | `conflict` | already exists; the run already finished; the deployment is in use |
| 413 | `too_large` | documents are limited to 32 MiB |
| 422 | `invalid_request` | validation; `errors` lists `loc`, `msg`, `type` (inputs are not echoed) |
| 503 | `unavailable` | starting or stopping; a deployment's credential is not set |
| 500 | `error` | unexpected; the response reveals no internals |

## Compatibility

Within `/v1`, response fields are only added. Clients should ignore fields they don't know; the server rejects unknown request fields.

## Long-running work

`POST /v1/runs` answers **202** with the run summary and `Location: /v1/runs/{id}`. The run executes in the server, independent of any connection. Follow it with either:

**Server-Sent Events**, `GET /v1/runs/{id}/stream[?after=N]`:

```
id: 7
event: run_event
data: {"sequence": 7, "type": "tool_call_decided", "occurred_at": "…", "summary": "fs_write_file: require approval (…)", "payload": {…}}

: keep-alive

event: end
data: {"run_id": "…", "phase": "completed", …}
```

Events arrive in order with `id` equal to the event sequence. Reconnect with `Last-Event-ID` (browsers do it automatically) to continue without gaps. Idle streams receive a comment at least every 15 seconds. The stream ends after the run finishes; while it waits for approval it stays open.

**Long polling**, `GET /v1/runs/{id}/summary?wait_s=30` (at most 60): returns when nothing is driving the run any more (finished, waiting for approval, or stopped) or when the wait ends. Repeat while `phase` is `running`.

Run phases: `queued`, `running`, `waiting_approval`, `completed`, `failed`, `cancelled`, `interrupted` (nothing is driving it; `POST …/resume`).

## Endpoints

Roles: **A** admin, **O** operator or admin, **V** anyone authenticated (results filtered by what you may see).

### Identity and system

| | | |
|---|---|---|
| `GET /v1/ping` | none | liveness only |
| `POST /v1/session` | none | `{"token"}` (API or single-use login token) → session cookie |
| `GET /v1/session` · `DELETE /v1/session` | V | current principal and expiry · sign out |
| `POST /v1/session/link` | V | `{"token"}`: a single-use, ten-minute sign-in token for yourself |
| `GET /v1/me` | V | your principal and grants |
| `GET /v1/health` | V | status, version, storage, counts, problems |
| `GET /v1/settings` | V | workspace, effective configuration (credentials as names only) |
| `PATCH /v1/settings/config` | A | merge changes into `hollis.toml`; validated before saving |
| `PUT /v1/settings/config` | A | replace the configuration |
| `GET /v1/audit?limit=` | A | the append-only audit log |
| `GET /v1/principals` · `POST /v1/principals` | A | |
| `GET /v1/tokens` · `POST /v1/tokens` · `DELETE /v1/tokens/{id}` | V | your tokens (admins: all, and for others). The value is in the `POST` response only |

### Agents, runs and approvals

| | | |
|---|---|---|
| `GET /v1/agents` · `GET /v1/agents/{name}` | V | |
| `POST /v1/agents` · `PUT /v1/agents/{name}` · `DELETE /v1/agents/{name}` | A | each update is a new version |
| `POST /v1/agents/starter` | A | create the starter `docs` collection and default agent (idempotent) |
| `POST /v1/agents/from-template` | A | `{"template", "name", "model", "parameters"?, "overrides"?}` → 201 with the agent and its `origin`; 422 with the reasons when a hard requirement fails ([specialists](../guides/specialists.md)) |
| `GET /v1/agents/{name}/upgrade` · `POST /v1/agents/{name}/upgrade` | A | the preview (`changes`, `kept`, `to_digest`, `checks`) · `{"digest", "reset"?}` applies it; 409 if the template's digest moved since the preview |
| `POST /v1/agents/{name}/export` | A | `{"id"?, "version"?}` → `{"filename", "content"}`: the agent as a template (TOML) |
| `GET /v1/runs?phase=&agent=&roots=&limit=&offset=` | V | newest first: `{"items": [...], "total"}`; `roots=true` leaves out delegated runs. Summaries carry `parent_run_id`, `root_run_id`, `depth`, `children` and a delegated run's `objective` |
| `POST /v1/runs` | O | `{"input", "agent"?, "session"?}`; or `{"input", "plan": {"steps": [{"id", "agent", "objective", "depends_on"?, ...}], "answer"?}}` to run a fixed plan on the workflow coordinator; admins may give `model`, `tools`, `instructions` instead of `agent`. Without either, the default agent |
| `GET /v1/runs/{id}/tree` | V | the orchestration the run belongs to: every run delegated below the root that you can see (pre-order), each run's report, refusals, disagreements, totals and limits ([guide](../guides/orchestration.md)) |
| `GET /v1/runs/{id}` | V | everything computed from the event log: turns, tool calls, approvals, answer, grounding, access, failure; the overlay rules it was held to (`policy`) and, under a contract, its `output` (`status` accepted, rejected or missing, `data`, `problems`); for a delegated run its `lineage` (ancestors), `assignment` and `delegation` limits |
| `GET /v1/runs/{id}/summary[?wait_s=]` | V | |
| `GET /v1/runs/{id}/events?after=` · `GET /v1/runs/{id}/stream` | V | |
| `POST /v1/runs/{id}/cancel` · `POST /v1/runs/{id}/resume` | creator or A | cancelling also cancels every run it delegated |
| `GET /v1/runs/{id}/artifacts/{artifact}?offset=&length=` | V | |
| `GET /v1/approvals` | V | pending calls in runs you can see; calls in delegated runs carry `root_run_id`, `depth` and the agents' `lineage` |
| `POST /v1/runs/{id}/approvals/{invocation_id}` | approve permission, and the run visible to you | `{"approved", "reason"?}`. Invocation ids contain slashes and are used verbatim. Recorded with your name; applies only to that exact call |

### Specialization

| Endpoint | Who | |
|---|---|---|
| `GET /v1/templates` · `GET /v1/templates/{id}` | V | templates with readiness in this workspace (`ready`, `checks`, `agents`) · detail with procedure, tools, overlay rules, requirements and per-deployment `fits` |
| `GET /v1/capabilities` · `GET /v1/contracts` | V | capabilities (and whether their tools exist here) · output contracts, their rules and submission schemas |
| `GET /v1/skills` | V | Agent Skills in the workspace and folders that could not be loaded |

### Models and tools

| | | |
|---|---|---|
| `GET /v1/deployments` · `GET /v1/deployments/{id}` | V | status: `ready`, `missing_credential`, `blocked_by_routing`; credential variable name and whether it is set, never the value |
| `PUT /v1/deployments/{id}` · `DELETE /v1/deployments/{id}` | A | `{"deployment": {...}}` as in `hollis.toml` |
| `POST /v1/deployments/{id}/probe` | O | one tiny real request |
| `GET /v1/tools` | V | each tool with the policy's decision and matching rule |
| `GET /v1/tools/activity?limit=` | V | recent tool calls |
| `GET /v1/mcp/servers` · `POST /v1/mcp/servers/{name}/connect` | V · A | |

### Knowledge and memory

| | | |
|---|---|---|
| `GET /v1/knowledge/collections` · `POST` · `DELETE /v1/knowledge/collections/{name}` | V · A · A | |
| `GET /v1/knowledge/collections/{name}/sources[/{source_id}]` | V | documents you may see; detail with sections and chunks |
| `PUT /v1/knowledge/collections/{name}/documents/{key}?labels=&trust=&filename=` | A | the raw document as the body; labels and trust come from the request, never the content |
| `PATCH …/documents/{key}` · `DELETE …/documents/{key}` | A | `{"labels"}` · remove from the index |
| `POST /v1/knowledge/collections/{name}/sync` | A | `{"path", "labels"?, "trust"?}` within `knowledge.sync_roots` |
| `POST /v1/knowledge/search` | V | `{"query", "collections"?, "limit"?}`, with your access |
| `POST /v1/knowledge/reconcile` | A | |
| `GET /v1/memory?scope=&kind=&status=&query=&limit=` · `GET /v1/memory/scopes` · `GET /v1/memory/{id}` | V | scopes you may read |
| `POST /v1/memory` | O | `{"scope", "content", "kind"?, "subject"?, "key"?}`; scopes you may write |
| `POST /v1/memory/{id}/retract` · `POST /v1/memory/{id}/forget` | O | `{"reason"}`. Forgetting deletes the content from the memory store; run event logs that recalled it keep their copies |
| `POST /v1/memory/maintenance` | A | |

## Example

```bash
curl -s -H "Authorization: Bearer $HOLLIS_TOKEN" -H "Content-Type: application/json" \
  -d '{"input": "How do we roll back?", "agent": "assistant"}' http://127.0.0.1:7878/v1/runs
curl -sN -H "Authorization: Bearer $HOLLIS_TOKEN" http://127.0.0.1:7878/v1/runs/$RUN/stream
```
