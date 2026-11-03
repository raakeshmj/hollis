"""The SDK: the same calls, results and errors embedded or over HTTP (in process, through the
ASGI transport), plus the synchronous client."""

from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest

from hollis import AsyncHollis, Hollis
from hollis.platform.auth import Principal
from hollis.platform.config import initialize
from hollis.platform.errors import Forbidden, InvalidRequest, NotFound
from hollis.platform.schemas import PrincipalInput, RunPhase, TokenInput
from hollis.platform.services import Platform
from hollis.sdk.backend import LocalBackend
from hollis.server import create_app

TOOLS = ("fs_write_file", "fs_list_dir", "knowledge_search", "memory_remember", "memory_recall")


@pytest.fixture(params=["embedded", "remote"])
async def client(
    request: pytest.FixtureRequest, platform: Platform, admin: Principal
) -> AsyncIterator[AsyncHollis]:
    """The same workspace through either backend."""
    if request.param == "embedded":
        sdk = AsyncHollis(LocalBackend(platform, admin, owned=False))
    else:
        token = (await platform.create_token(admin, TokenInput(principal="admin"))).token
        transport = httpx2.ASGITransport(app=create_app(platform=platform))
        sdk = AsyncHollis.connect("http://127.0.0.1:7878", token, transport=transport)
    try:
        yield sdk
    finally:
        await sdk.close()


async def test_a_run_from_start_through_approval_to_the_answer(
    client: AsyncHollis, platform: Platform
) -> None:
    await client.agents.create(name="helper", model="demo", tools=TOOLS)
    run = await client.runs.start("write out.txt: from the sdk", agent="helper")

    waiting = await run.wait(timeout_s=10)
    assert waiting.phase is RunPhase.WAITING_APPROVAL
    [approval] = await run.approvals()
    assert approval.tool == "fs_write_file"
    await run.approve(reason="looks right")
    assert (await run.wait(timeout_s=10)).phase is RunPhase.COMPLETED

    assert (platform.ws.root / "out.txt").read_text() == "from the sdk"
    answer = await run.answer()
    assert answer is not None
    assert answer.startswith("[demo]")
    events = await run.events()
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    streamed = [e async for e in run.stream(after=len(events) - 3)]
    assert [e.sequence for e in streamed] == [len(events) - 2, len(events) - 1, len(events)]
    listed = await client.runs.list(phase=RunPhase.COMPLETED)
    assert [r.run_id for r in listed.items] == [run.id]
    assert (await client.runs.get(str(run.id))).phase is RunPhase.COMPLETED


async def test_denying_and_cancelling(client: AsyncHollis) -> None:
    await client.agents.create(name="helper", model="demo", tools=TOOLS)
    denied = await client.runs.start("write a.txt: no", agent="helper")
    await denied.wait(timeout_s=10)
    await denied.deny(reason="not now")
    assert (await denied.wait(timeout_s=10)).phase is RunPhase.COMPLETED

    cancelled = await client.runs.start("write b.txt: no", agent="helper")
    await cancelled.wait(timeout_s=10)
    assert (await cancelled.cancel("changed plans")).phase is RunPhase.CANCELLED
    with pytest.raises(InvalidRequest):
        await cancelled.approve()  # nothing is pending any more


async def test_knowledge_memory_models_and_tools(client: AsyncHollis) -> None:
    await client.knowledge.create_collection("docs")
    ingested = await client.knowledge.ingest(
        "docs", "guide.md", "# Guide\n\nRotate keys every 90 days.\n"
    )
    assert ingested.action == "created"
    hits = (await client.knowledge.search("rotate keys")).hits
    assert hits[0].source_key == "guide.md"
    assert [s.key for s in await client.knowledge.sources("docs")] == ["guide.md"]

    written = await client.memory.remember("Deploys happen on Tuesdays.", scope="team:ops")
    assert written.memory is not None
    assert [m.content for m in await client.memory.list(scope="team:ops")] == [
        "Deploys happen on Tuesdays."
    ]
    await client.memory.forget(written.memory.memory_id, reason="test")
    assert await client.memory.list(scope="team:ops", status="active") == []

    assert {d.id for d in await client.models.list()} >= {"demo"}
    assert (await client.models.probe("demo")).ok
    assert "fs_write_file" in {t.name for t in await client.tools.list()}
    assert (await client.health()).status == "ok"
    assert (await client.whoami()).name == "admin"


async def test_errors_are_the_same_types_in_both_modes(client: AsyncHollis) -> None:
    with pytest.raises(NotFound):
        await client.runs.get("00000000-0000-4000-8000-000000000000")
    with pytest.raises(NotFound):
        await client.agents.get("nobody")
    with pytest.raises(InvalidRequest):
        await client.runs.get("not-a-run-id")
    with pytest.raises(InvalidRequest):
        await client.runs.start("x", model="no-such-model")


async def test_a_remote_client_acts_only_as_its_token(platform: Platform, admin: Principal) -> None:
    await platform.create_principal(admin, PrincipalInput(name="olive"))
    token = (await platform.create_token(admin, TokenInput(principal="olive"))).token
    transport = httpx2.ASGITransport(app=create_app(platform=platform))
    async with AsyncHollis.connect("http://127.0.0.1:7878", token, transport=transport) as olive:
        assert (await olive.whoami()).name == "olive"
        with pytest.raises(Forbidden):
            await olive.agents.create(name="mine", model="demo")
        created = await olive.tokens.create(label="second")
        assert created.principal == "olive"


def test_the_synchronous_client_embeds_a_workspace(tmp_path: Path) -> None:
    initialize(tmp_path, "sync")
    with Hollis.open(tmp_path) as hollis:
        hollis.agents.starter()
        run = hollis.runs.start("write s.txt: sync")
        assert run.wait(timeout_s=10).phase is RunPhase.WAITING_APPROVAL
        run.approve()
        assert run.wait(timeout_s=10).phase is RunPhase.COMPLETED
        assert [e.type for e in run.stream()][-1] == "run_completed"
        assert hollis.location == str(tmp_path.resolve())
    assert (tmp_path / "s.txt").read_text() == "sync"
    with Hollis.open(tmp_path) as again:  # the state is in the workspace, not the process
        assert again.runs.list().total == 1


def test_opening_without_a_workspace_explains_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HOLLIS_WORKSPACE", raising=False)
    monkeypatch.delenv("HOLLIS_URL", raising=False)
    with pytest.raises(NotFound, match="hollis init"):
        Hollis.from_env()


class DropsFirstStream(httpx2.AsyncBaseTransport):
    """Fails the first event-stream request as a dropped connection would."""

    def __init__(self, inner: httpx2.AsyncBaseTransport) -> None:
        self.inner = inner
        self.dropped = 0
        self.resumed_from: list[str] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/stream"):
            self.resumed_from.append(request.headers.get("last-event-id", ""))
            if not self.dropped:
                self.dropped += 1
                raise httpx2.ReadError("connection reset", request=request)
        return await self.inner.handle_async_request(request)


async def test_following_a_run_survives_a_dropped_connection(
    platform: Platform, admin: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hollis.sdk import remote

    monkeypatch.setattr(remote, "RECONNECT_DELAYS_S", (0.0, 0.0))
    await platform.create_starter(admin)
    token = (await platform.create_token(admin, TokenInput(principal="admin"))).token
    transport = DropsFirstStream(httpx2.ASGITransport(app=create_app(platform=platform)))
    async with AsyncHollis.connect("http://127.0.0.1:7878", token, transport=transport) as sdk:
        run = await sdk.runs.start("list files")
        await run.wait(timeout_s=10)
        events = [e async for e in run.stream(after=2)]

    assert transport.dropped == 1
    assert transport.resumed_from == ["2", "2"], "the retry resumes after the last event seen"
    assert events[0].sequence == 3
    assert events[-1].type == "run_completed"


async def test_orchestration_through_the_sdk(client: AsyncHollis, platform: Platform) -> None:
    from hollis import Plan, PlanStep

    await client.agents.create(name="researcher", model="demo")
    await client.agents.create(name="lead", model="demo", delegates=("researcher",))
    run = await client.runs.start("@researcher what is hollis?", agent="lead")
    assert (await run.wait(timeout_s=20)).phase is RunPhase.COMPLETED
    tree = await run.tree()
    assert [n.run.agent for n in tree.nodes] == ["lead", "researcher"]
    assert tree.nodes[1].report is not None
    roots = await client.runs.list(roots=True)
    assert [r.run_id for r in roots.items] == [run.id]

    plan = Plan(steps=(PlanStep(id="ask", agent="researcher", objective="what is hollis?"),))
    planned = await client.runs.start("Plan it", plan=plan)
    assert (await planned.wait(timeout_s=20)).phase is RunPhase.COMPLETED
    assert planned.summary.model == "workflow"
    assert len((await planned.tree()).nodes) == 2


async def test_specialists_through_the_sdk(client: AsyncHollis, platform: Platform) -> None:
    (platform.ws.root / "app.py").write_text("print('hello')\n")
    gallery = {t.id: t for t in await client.templates.list()}
    assert gallery["code-reviewer"].ready
    assert (await client.templates.get("code-reviewer")).contract == "code-review"
    assert {c.id for c in await client.templates.capabilities()} >= {"read-only"}
    assert {c.id for c in await client.templates.contracts()} >= {"code-review"}
    assert (await client.templates.skills()).skills == ()

    agent = await client.agents.create_from_template(
        "code-reviewer", "reviewer", "demo", parameters={"focus": "security"}
    )
    assert agent.origin is not None
    assert "security" in agent.instructions
    with pytest.raises(InvalidRequest, match="knowledge"):
        await client.agents.create_from_template("researcher", "r", "demo")

    run = await client.runs.start("Review app.py", agent="reviewer")
    await run.wait(timeout_s=10)
    output = await run.output()
    assert output is not None
    assert output.status == "accepted"

    customized = await client.agents.customize("reviewer", description="Ours.")
    assert customized.origin is not None
    assert customized.origin.overrides == ("description",)
    preview = await client.agents.upgrade_preview("reviewer")
    assert preview.up_to_date
    assert preview.kept == ("description",)
    upgraded = await client.agents.upgrade("reviewer", digest=preview.to_digest)
    assert upgraded.description == "Ours."
    exported = await client.agents.export_template("reviewer", id="our-review")
    assert exported.content.startswith("# Hollis agent template")


def test_the_synchronous_client_makes_specialists(tmp_path: Path) -> None:
    initialize(tmp_path, "sync")
    with Hollis.open(tmp_path) as hollis:
        assert any(t.id == "data-analyst" for t in hollis.templates.list())
        (tmp_path / "sales.csv").write_text("region,amount\nnorth,1\n")
        hollis.agents.create_from_template("data-analyst", "analyst", "demo")
        run = hollis.runs.start("How many sales?", agent="analyst")
        run.wait(timeout_s=10)
        output = run.output()
        assert output is not None
        assert output.status == "accepted"
        assert hollis.agents.export_template("analyst", id="our-analyst").filename == (
            "our-analyst.toml"
        )
