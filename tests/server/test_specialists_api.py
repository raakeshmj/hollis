"""Specialization over HTTP: the gallery, creating from a template, upgrades, export, the run's
structured output, and who may do what."""

import httpx2

from hollis.platform.auth import Principal
from hollis.platform.schemas import Role, TokenInput
from hollis.platform.services import Platform
from tests.support.platform import add_principal


async def _finished(client: httpx2.AsyncClient, run_id: str) -> dict[str, object]:
    while True:
        summary = (await client.get(f"/v1/runs/{run_id}/summary", params={"wait_s": 5})).json()
        if summary["phase"] not in ("queued", "running"):
            return summary


async def test_templates_and_a_specialist_run_over_http(
    client: httpx2.AsyncClient, platform: Platform
) -> None:
    (platform.ws.root / "app.py").write_text("print('hello')\n")
    gallery = (await client.get("/v1/templates")).json()
    assert {t["id"] for t in gallery} >= {"code-reviewer", "software-engineer", "researcher"}
    detail = (await client.get("/v1/templates/code-reviewer")).json()
    assert detail["contract"] == "code-review"
    assert any(f["deployment"] == "demo" and f["ok"] for f in detail["fits"])
    assert (await client.get("/v1/templates/nope")).status_code == 404
    assert {c["id"] for c in (await client.get("/v1/capabilities")).json()} >= {"read-only"}
    assert {c["tool"] for c in (await client.get("/v1/contracts")).json()} >= {"submit_code_review"}
    assert (await client.get("/v1/skills")).json()["skills"] == []

    created = await client.post(
        "/v1/agents/from-template",
        json={"template": "code-reviewer", "name": "reviewer", "model": "demo"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["origin"]["template"] == "code-reviewer"
    refused = await client.post(
        "/v1/agents/from-template",
        json={"template": "researcher", "name": "r", "model": "demo"},
    )
    assert refused.status_code == 422
    assert "knowledge" in refused.json()["detail"]

    started = await client.post("/v1/runs", json={"input": "Review app.py", "agent": "reviewer"})
    run_id = started.json()["run_id"]
    assert (await _finished(client, run_id))["phase"] == "completed"
    run = (await client.get(f"/v1/runs/{run_id}")).json()
    assert run["output"]["status"] == "accepted"
    assert run["output"]["data"]["scope"] == ["app.py"]
    assert [rule["name"] for rule in run["policy"]] == ["read-only"]

    preview = (await client.get("/v1/agents/reviewer/upgrade")).json()
    assert preview["up_to_date"] is True
    stale = await client.post("/v1/agents/reviewer/upgrade", json={"digest": "sha256:old"})
    assert stale.status_code == 409
    exported = (await client.post("/v1/agents/reviewer/export", json={"id": "team-review"})).json()
    assert exported["filename"] == "team-review.toml"
    assert 'contract = "code-review"' in exported["content"]


async def test_viewers_read_templates_but_only_admins_create_from_them(
    anonymous: httpx2.AsyncClient, platform: Platform, admin: Principal
) -> None:
    await add_principal(platform, admin, "vera", Role.VIEWER)
    token = await platform.create_token(admin, TokenInput(principal="vera", label="t"))
    anonymous.headers["authorization"] = f"Bearer {token.token}"
    assert (await anonymous.get("/v1/templates")).status_code == 200
    denied = await anonymous.post(
        "/v1/agents/from-template",
        json={"template": "code-reviewer", "name": "reviewer", "model": "demo"},
    )
    assert denied.status_code == 403
    assert (await anonymous.post("/v1/agents/x/export", json={})).status_code == 403
