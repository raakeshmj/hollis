"""The REST API over the platform: contract, authentication, errors and a full journey over
HTTP. In process through httpx2's ASGI transport; tests/server/test_live_server.py uses a real
socket."""

import json
from typing import Any, cast

import httpx2
import pytest

from hollis.platform.auth import Principal
from hollis.platform.schemas import PrincipalInput, Role, TokenInput
from hollis.platform.services import Platform
from hollis.server import app as server_app
from tests.server.conftest import BASE
from tests.support.platform import add_agent


def _data(event: httpx2.ServerSentEvent) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(event.data))


def _problem(response: httpx2.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = response.json()
    assert (body["status"], body["code"], body["type"]) == (
        status,
        code,
        f"urn:hollis:problem:{code}",
    )
    return body


async def test_everything_but_ping_needs_authentication(anonymous: httpx2.AsyncClient) -> None:
    assert (await anonymous.get("/v1/ping")).json() == {"status": "ok"}
    for path in ("/v1/health", "/v1/runs", "/v1/agents", "/v1/approvals", "/v1/memory"):
        response = await anonymous.get(path)
        _problem(response, 401, "unauthenticated")
        assert response.headers["www-authenticate"].startswith("Bearer")
    _problem(
        await anonymous.get("/v1/me", headers={"authorization": "Bearer nope"}),
        401,
        "unauthenticated",
    )
    _problem(
        await anonymous.get("/v1/me", headers={"authorization": "Basic YWRtaW4="}),
        401,
        "unauthenticated",
    )


async def test_openapi_describes_the_versioned_api(anonymous: httpx2.AsyncClient) -> None:
    spec = (await anonymous.get("/v1/openapi.json")).json()
    assert spec["info"]["version"] == "1"
    assert {"/v1/runs", "/v1/runs/{run_id}/stream", "/v1/approvals"} <= set(spec["paths"])
    assert "RunDetail" in spec["components"]["schemas"]
    assert "swagger" not in (await anonymous.get("/docs")).text.lower(), "no CDN-loaded docs page"


async def test_responses_carry_security_headers_and_no_cors(client: httpx2.AsyncClient) -> None:
    response = await client.get("/v1/health", headers={"origin": "http://evil.example"})
    assert response.status_code == 200
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert not any(h.startswith("access-control-") for h in response.headers)


async def test_unexpected_host_headers_are_refused(client: httpx2.AsyncClient) -> None:
    response = await client.get("/v1/ping", headers={"host": "attacker.example"})
    _problem(response, 400, "bad_host")
    assert (await client.get("/v1/ping", headers={"host": "localhost:7878"})).status_code == 200


async def test_a_run_over_http_from_start_to_approval_to_result(
    client: httpx2.AsyncClient, platform: Platform, admin: Principal
) -> None:
    created = await client.post(
        "/v1/agents",
        json={"name": "helper", "model": "demo", "tools": ["fs_write_file", "fs_list_dir"]},
    )
    assert created.status_code == 201
    started = await client.post(
        "/v1/runs", json={"input": "write hi.txt: hello", "agent": "helper"}
    )
    assert started.status_code == 202
    run_id = started.json()["run_id"]
    assert started.headers["location"] == f"/v1/runs/{run_id}"

    waiting = (await client.get(f"/v1/runs/{run_id}/summary", params={"wait_s": 10})).json()
    assert waiting["phase"] == "waiting_approval"
    [approval] = (await client.get("/v1/approvals")).json()
    decided = await client.post(
        f"/v1/runs/{run_id}/approvals/{approval['invocation_id']}",
        json={"approved": True, "reason": "fine"},
    )
    assert decided.status_code == 200
    done = (await client.get(f"/v1/runs/{run_id}/summary", params={"wait_s": 10})).json()
    assert done["phase"] == "completed"
    assert (platform.ws.root / "hi.txt").read_text() == "hello"

    detail = (await client.get(f"/v1/runs/{run_id}")).json()
    assert detail["answer"].startswith("[demo]")
    assert detail["turns"][0]["tool_calls"][0]["approver"] == "admin"
    events = (await client.get(f"/v1/runs/{run_id}/events", params={"after": 2})).json()
    assert events[0]["sequence"] == 3
    assert events[-1]["type"] == "run_completed"


async def test_the_event_stream_replays_and_ends_with_the_summary(
    client: httpx2.AsyncClient, platform: Platform, admin: Principal
) -> None:
    await add_agent(platform, admin)
    run_id = (
        await client.post("/v1/runs", json={"input": "list files", "agent": "helper"})
    ).json()["run_id"]
    await client.get(f"/v1/runs/{run_id}/summary", params={"wait_s": 10})

    seen: list[httpx2.ServerSentEvent] = []
    async with client.stream(
        "GET", f"/v1/runs/{run_id}/stream", headers={"last-event-id": "2"}
    ) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        async for event in httpx2.EventSource(response):
            seen.append(event)

    assert seen[0].event == "run_event"
    assert seen[0].id == "3"
    assert _data(seen[-2])["type"] == "run_completed"
    end = seen[-1]
    assert end.event == "end"
    assert _data(end)["phase"] == "completed"


async def test_browser_sessions_use_strict_cookies_and_refuse_cross_origin_changes(
    anonymous: httpx2.AsyncClient, platform: Platform, admin: Principal
) -> None:
    await add_agent(platform, admin)
    login = await platform.login_token()
    response = await anonymous.post("/v1/session", json={"token": login})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie
    assert (await anonymous.get("/v1/me")).json()["name"] == "admin"
    run = {"input": "hello", "agent": "helper"}

    _problem(await anonymous.post("/v1/runs", json=run), 403, "forbidden")  # no Origin
    evil = {"origin": "http://evil.example"}
    _problem(await anonymous.post("/v1/runs", json=run, headers=evil), 403, "forbidden")
    same = {"origin": BASE}
    assert (await anonymous.post("/v1/runs", json=run, headers=same)).status_code == 202

    _problem(await anonymous.post("/v1/session", json={"token": login}), 401, "unauthenticated")
    assert (await anonymous.delete("/v1/session", headers=same)).status_code == 204
    _problem(await anonymous.get("/v1/me"), 401, "unauthenticated")


async def test_platform_errors_map_to_http_statuses(
    client: httpx2.AsyncClient, platform: Platform, admin: Principal
) -> None:
    await add_agent(platform, admin)
    await platform.create_principal(admin, PrincipalInput(name="olive", role=Role.OPERATOR))
    olive = (await platform.create_token(admin, TokenInput(principal="olive"))).token

    _problem(await client.get("/v1/runs/00000000-0000-4000-8000-000000000000"), 404, "not_found")
    _problem(
        await client.post("/v1/agents", json={"name": "helper", "model": "demo"}), 409, "conflict"
    )
    forbidden = await client.post(
        "/v1/agents",
        json={"name": "mine", "model": "demo"},
        headers={"authorization": f"Bearer {olive}"},
    )
    _problem(forbidden, 403, "forbidden")
    body = _problem(
        await client.post("/v1/agents", json={"name": "Bad Name", "model": "demo"}),
        422,
        "invalid_request",
    )
    assert body["errors"][0]["loc"] == ["body", "name"]
    assert "input" not in body["errors"][0]
    _problem(await client.get("/v1/runs/not-a-uuid"), 422, "invalid_request")
    _problem(await client.get("/v1/no-such-route"), 404, "not_found")
    _problem(await client.delete("/v1/ping"), 405, "method_not_allowed")


async def test_unexpected_failures_reveal_nothing(
    client: httpx2.AsyncClient, platform: Platform, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: object) -> None:
        raise RuntimeError("internal detail: /secret/path")

    monkeypatch.setattr(platform, "health", broken)
    # The server re-raises after answering (so it is logged); this client does not re-raise it.
    transport = httpx2.ASGITransport(
        app=server_app.create_app(platform=platform), raise_app_exceptions=False
    )
    async with httpx2.AsyncClient(
        transport=transport, base_url=BASE, headers=client.headers
    ) as raw:
        body = _problem(await raw.get("/v1/health"), 500, "error")
    assert "secret" not in str(body)


async def test_documents_are_uploaded_raw_with_labels_from_the_request(
    client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        await client.post("/v1/knowledge/collections", json={"name": "docs"})
    ).status_code == 201
    text = b"# Runbook\n\nRestart the worker with `make restart`.\n"
    put = await client.put(
        "/v1/knowledge/collections/docs/documents/ops/runbook.md",
        content=text,
        params={"labels": ["team:ops"], "trust": "internal"},
    )
    assert put.status_code == 200, put.text
    assert put.json()["action"] == "created"
    source = put.json()["source"]
    assert (source["key"], source["labels"]) == ("ops/runbook.md", ["team:ops"])

    detail = await client.get(f"/v1/knowledge/collections/docs/sources/{source['source_id']}")
    assert detail.json()["chunks"][0]["text"].startswith("Restart the worker")
    hits = (await client.post("/v1/knowledge/search", json={"query": "restart worker"})).json()[
        "hits"
    ]
    assert hits[0]["source_key"] == "ops/runbook.md"
    relabel = await client.patch(
        "/v1/knowledge/collections/docs/documents/ops/runbook.md", json={"labels": ["team:sre"]}
    )
    assert relabel.json()["labels"] == ["team:sre"]

    monkeypatch.setattr(server_app, "MAX_UPLOAD_BYTES", 8)
    too_big = await client.put("/v1/knowledge/collections/docs/documents/big.md", content=b"x" * 64)
    _problem(too_big, 413, "too_large")
    deleted = await client.delete("/v1/knowledge/collections/docs/documents/ops/runbook.md")
    assert deleted.status_code == 204


async def test_secrets_are_never_returned(
    client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOLLIS_TEST_KEY", "sk-live-should-never-appear-0123456789")
    deployment = {
        "id": "remote",
        "protocol": "openai-chat-completions",
        "provider": "example",
        "model": "m",
        "locality": "external",
        "endpoint": {
            "base_url": "https://llm.example/v1",
            "auth": {"type": "bearer", "env": "HOLLIS_TEST_KEY"},
        },
    }
    put = await client.put("/v1/deployments/remote", json={"deployment": deployment})
    assert put.status_code == 200, put.text
    assert put.json()["credential"] == {"env": "HOLLIS_TEST_KEY", "present": True}
    bodies = [
        (await client.get(path)).text
        for path in ("/v1/deployments", "/v1/settings", "/v1/health", "/v1/deployments/remote")
    ]
    assert not any("sk-live-should-never-appear" in body for body in bodies)
    mismatch = await client.put("/v1/deployments/other", json={"deployment": deployment})
    _problem(mismatch, 422, "invalid_request")


async def test_the_web_app_is_served_for_non_api_paths(anonymous: httpx2.AsyncClient) -> None:
    page = await anonymous.get("/runs/some-id")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "content-security-policy" in page.headers
    escaped = await anonymous.get("/assets/..%2f..%2f..%2fpyproject.toml")
    assert "[project]" not in escaped.text
