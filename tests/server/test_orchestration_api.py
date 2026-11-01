"""Orchestration over HTTP: plans on POST /v1/runs, the tree, lineage, the roots filter, and
visibility of other principals' orchestrations."""

import httpx2

from hollis.platform.auth import Principal
from hollis.platform.schemas import TokenInput
from hollis.platform.services import Platform
from tests.support.orchestration import add_team
from tests.support.platform import add_principal

PLAN = {
    "steps": [
        {"id": "deploy", "agent": "researcher", "objective": "how do we deploy?"},
        {"id": "check", "agent": "reviewer", "objective": "check it", "depends_on": ["deploy"]},
    ]
}


async def _finished(client: httpx2.AsyncClient, run_id: str) -> dict[str, object]:
    while True:
        summary = (await client.get(f"/v1/runs/{run_id}/summary", params={"wait_s": 5})).json()
        if summary["phase"] not in ("queued", "running"):
            return summary


async def test_a_plan_run_and_its_tree_over_http(
    client: httpx2.AsyncClient, platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin)
    started = await client.post("/v1/runs", json={"input": "Brief", "plan": PLAN})
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]
    assert (await _finished(client, run_id))["phase"] == "completed"

    tree = (await client.get(f"/v1/runs/{run_id}/tree")).json()
    assert [n["run"]["depth"] for n in tree["nodes"]] == [0, 1, 1]
    deploy, check = tree["nodes"][1:]
    assert check["inputs"] == [deploy["run"]["run_id"]]
    assert deploy["report"]["outcome"] == "completed"
    assert tree["totals"]["runs"] == 3

    child = (await client.get(f"/v1/runs/{check['run']['run_id']}")).json()
    assert child["assignment"]["agent"] == "reviewer"
    assert [step["run_id"] for step in child["lineage"]] == [run_id]
    from_child = (await client.get(f"/v1/runs/{check['run']['run_id']}/tree")).json()
    assert from_child["root_run_id"] == run_id

    roots = (await client.get("/v1/runs", params={"roots": "true"})).json()
    assert [r["run_id"] for r in roots["items"]] == [run_id]
    assert roots["items"][0]["children"] == 2
    assert (await client.get("/v1/runs")).json()["total"] == 3


async def test_invalid_plans_are_problems(
    client: httpx2.AsyncClient, platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin)
    cycle = {
        "steps": [
            {"id": "a", "agent": "researcher", "objective": "x", "depends_on": ["b"]},
            {"id": "b", "agent": "researcher", "objective": "x", "depends_on": ["a"]},
        ]
    }
    response = await client.post("/v1/runs", json={"input": "x", "plan": cycle})
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    both = await client.post("/v1/runs", json={"input": "x", "plan": PLAN, "agent": "lead"})
    assert both.status_code == 422
    assert "only an input and the plan" in both.json()["detail"]


async def test_other_principals_orchestrations_are_not_found(
    client: httpx2.AsyncClient, anonymous: httpx2.AsyncClient, platform: Platform, admin: Principal
) -> None:
    await add_team(platform, admin)
    run_id = (await client.post("/v1/runs", json={"input": "Brief", "plan": PLAN})).json()["run_id"]
    await _finished(client, run_id)
    await add_principal(platform, admin, "olive")
    token = await platform.create_token(admin, TokenInput(principal="olive"))
    olive = {"authorization": f"Bearer {token.token}"}
    response = await anonymous.get(f"/v1/runs/{run_id}/tree", headers=olive)
    assert response.status_code == 404
