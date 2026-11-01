# ADR-0027: REST API contract, long-running work and the SDK

- Status: Accepted (M6)
- Date: 2026-11-01

## Context

The REST API must expose the SDK's operations (FR-11), serve a web app, and handle runs that take minutes or wait days for an approval, over connections that drop.

## Decision

**Contract.**
- FastAPI over the platform services (optional extra `hollis[server]`), versioned under `/v1`, described by OpenAPI at `/v1/openapi.json` (generated from the same pydantic models the services return).
- Within version 1, fields are only added. Responses ignore unknown fields (older clients keep working); requests reject them.
- Errors are RFC 9457 problem details (`application/problem+json`) with a stable `code` (`invalid_request` 422, `unauthenticated` 401, `forbidden` 403, `not_found` 404, `conflict` 409, `unavailable` 503) and type `urn:hollis:problem:<code>`. Validation errors list locations without echoing inputs; unexpected errors reveal nothing.
- Resources the caller may not see answer 404, not 403, so identifiers reveal nothing.

**Long-running work.** `POST /v1/runs` answers 202 with a `Location`. Clients follow with:
- Server-Sent Events, `GET /v1/runs/{id}/stream`: `run_event` events (id = event sequence), keep-alive comments, one final `end` event with the summary; resumable with `Last-Event-ID`.
- Long polling, `GET /v1/runs/{id}/summary?wait_s=…` (at most 60 s per request), returning when nothing drives the run any more.

Runs never depend on a connection.

**SDK.** `Hollis` (sync) and `AsyncHollis` share one typed API over two backends: `LocalBackend` (an embedded workspace, as its local admin) and `RemoteBackend` (the REST API, as the token's principal, over httpx2; extra `hollis[client]`). Both return the same models and raise the same `PlatformError` types. The remote event stream reconnects with `Last-Event-ID`. The sync client drives the async one on a private event-loop thread.

**The web app's types are generated** from the committed OpenAPI document (`make web-api`); a test fails when it no longer matches the server.

## Alternatives considered

- **WebSockets:** bidirectional channels are not needed; SSE works through proxies and resumes natively.
- **gRPC:** poor fit for browsers and scripts.
- **A hand-written SDK HTTP layer per call without a shared contract:** the shared models and error map keep both modes identical, and tests run every SDK call against both backends.

## Trade-offs

FastAPI and uvicorn are optional dependencies; the core stays light. Problem details and SSE are standards with good client support; long polling covers clients without SSE.

## Consequences

Tests: API contract, errors and journeys in process (`tests/server/test_api.py`), a real loopback server with live SSE and restart (`test_live_server.py`), every SDK operation against both backends, and stream reconnection after a dropped connection (`tests/sdk/`).
