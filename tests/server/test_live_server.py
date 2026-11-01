"""The server on a real loopback socket: startup and shutdown through the ASGI lifespan, live
Server-Sent Events while a run waits for approval, and state that survives a restart."""

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx2
import uvicorn

from hollis.platform.config import initialize
from hollis.platform.schemas import AgentInput, TokenInput
from hollis.platform.services import Platform
from hollis.server import create_app


@asynccontextmanager
async def serving(root: Path) -> AsyncGenerator[tuple[str, str]]:
    """Serve the workspace on an ephemeral port; yields (base URL, admin API token)."""
    tokens: list[str] = []

    async def ready(platform: Platform) -> None:
        admin = await platform.local_admin()
        tokens.append((await platform.create_token(admin, TokenInput(principal="admin"))).token)
        if not await platform.agents(admin):
            await platform.create_agent(
                admin, AgentInput(name="helper", model="demo", tools=("fs_write_file",))
            )

    config = uvicorn.Config(
        create_app(root=root, on_ready=ready),
        host="127.0.0.1",
        port=0,
        log_level="warning",
        ws="none",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:  # noqa: ASYNC110  # uvicorn exposes readiness only as a flag
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}", tokens[0]
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)


async def test_live_events_stream_while_a_run_waits_and_after_approval(tmp_path: Path) -> None:
    initialize(tmp_path, "live")
    async with serving(tmp_path) as (base, token):
        headers = {"authorization": f"Bearer {token}"}
        async with httpx2.AsyncClient(base_url=base, headers=headers, timeout=10) as client:
            run = await client.post("/v1/runs", json={"input": "write a.txt: x", "agent": "helper"})
            run_id = run.json()["run_id"]
            seen: list[httpx2.ServerSentEvent] = []

            async def follow() -> None:
                async with client.stream("GET", f"/v1/runs/{run_id}/stream") as response:
                    async for event in httpx2.EventSource(response):
                        seen.append(event)

            follower = asyncio.create_task(follow())
            for _ in range(500):  # events arrive while the stream is still open
                if any(e.event == "run_event" and "tool_call_decided" in e.data for e in seen):
                    break
                await asyncio.sleep(0.01)
            assert not follower.done(), "the stream stays open while the run waits for approval"
            [approval] = (await client.get("/v1/approvals")).json()
            await client.post(
                f"/v1/runs/{run_id}/approvals/{approval['invocation_id']}", json={"approved": True}
            )
            await asyncio.wait_for(follower, timeout=10)

    assert seen[-1].event == "end"
    assert json.loads(seen[-1].data)["phase"] == "completed"
    run_events = [e for e in seen if e.event == "run_event"]
    assert [int(e.id) for e in run_events] == list(range(1, len(run_events) + 1))
    assert (tmp_path / "a.txt").read_text() == "x"


async def test_state_survives_a_server_restart(tmp_path: Path) -> None:
    initialize(tmp_path, "live")
    async with (
        serving(tmp_path) as (base, token),
        httpx2.AsyncClient(base_url=base, headers={"authorization": f"Bearer {token}"}) as client,
    ):
        run = await client.post("/v1/runs", json={"input": "write b.txt: y", "agent": "helper"})
        run_id = run.json()["run_id"]
        waiting = await client.get(f"/v1/runs/{run_id}/summary", params={"wait_s": 10})
        assert waiting.json()["phase"] == "waiting_approval"

    async with (
        serving(tmp_path) as (base, token),
        httpx2.AsyncClient(base_url=base, headers={"authorization": f"Bearer {token}"}) as client,
    ):
        [approval] = (await client.get("/v1/approvals")).json()
        assert approval["run_id"] == run_id
        await client.post(
            f"/v1/runs/{run_id}/approvals/{approval['invocation_id']}", json={"approved": True}
        )
        done = await client.get(f"/v1/runs/{run_id}/summary", params={"wait_s": 10})
        assert done.json()["phase"] == "completed"
