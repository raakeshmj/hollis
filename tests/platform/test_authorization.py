"""Authorization is enforced in the Platform API, so every interface inherits it (ADR-0028)."""

from collections.abc import Awaitable

import pytest

from hollis.platform.auth import Principal
from hollis.platform.config import ServerConfig
from hollis.platform.errors import Forbidden, NotFound, PlatformError
from hollis.platform.schemas import (
    AgentInput,
    ApprovalDecisionInput,
    CancelInput,
    CollectionInput,
    DeploymentInput,
    PrincipalInput,
    RememberInput,
    Role,
    RunPhase,
    SearchInput,
    StartRun,
    TokenInput,
)
from hollis.platform.services import Platform
from hollis.runtime.knowledge import KnowledgeAccess
from hollis.runtime.memory import MemoryAccess
from tests.support.platform import add_agent, add_principal, run_to_rest, write_readme


async def _raises[T](error: type[PlatformError], call: Awaitable[T]) -> None:
    with pytest.raises(error):
        await call


async def test_viewers_read_but_change_nothing(platform: Platform, admin: Principal) -> None:
    await add_agent(platform, admin)
    viewer = await add_principal(platform, admin, "vera", Role.VIEWER)

    assert [a.name for a in await platform.agents(viewer)] == ["helper"]
    await _raises(Forbidden, platform.start_run(viewer, StartRun(input="hi", agent="helper")))
    await _raises(
        Forbidden,
        platform.remember(viewer, RememberInput(scope="project:x", content="no")),
    )


async def test_only_admins_configure_the_workspace(platform: Platform, admin: Principal) -> None:
    operator = await add_principal(platform, admin, "olive")
    agent = AgentInput(name="mine", model="demo")
    deployment = DeploymentInput(deployment={"id": "x"})

    await _raises(Forbidden, platform.create_agent(operator, agent))
    await _raises(Forbidden, platform.delete_agent(operator, "helper"))
    await _raises(Forbidden, platform.upsert_deployment(operator, deployment))
    await _raises(Forbidden, platform.update_config(operator, {}))
    await _raises(Forbidden, platform.create_principal(operator, PrincipalInput(name="eve")))
    await _raises(Forbidden, platform.principals(operator))
    await _raises(Forbidden, platform.audit(operator))
    await _raises(Forbidden, platform.create_collection(operator, CollectionInput(name="c")))
    await _raises(Forbidden, platform.connect_mcp(operator, "none"))


async def test_operators_run_agents_but_not_ad_hoc_runs(
    platform: Platform, admin: Principal
) -> None:
    await add_agent(platform, admin)
    operator = await add_principal(platform, admin, "olive")

    done = await run_to_rest(platform, operator, "hello")
    assert done.phase is RunPhase.COMPLETED
    assert done.created_by == "olive"
    ad_hoc = StartRun(input="hi", model="demo", tools=("fs_read_file",))
    await _raises(Forbidden, platform.start_run(operator, ad_hoc))


async def test_runs_are_private_to_their_creator_by_default(
    platform: Platform, admin: Principal
) -> None:
    await add_agent(platform, admin)
    olive = await add_principal(platform, admin, "olive")
    oscar = await add_principal(platform, admin, "oscar")
    mine = await run_to_rest(platform, olive, "hello")

    assert (await platform.runs(oscar)).total == 0
    await _raises(NotFound, platform.run(oscar, mine.run_id))
    await _raises(NotFound, platform.run_events(oscar, mine.run_id))
    await _raises(NotFound, platform.cancel_run(oscar, mine.run_id, CancelInput()))
    await _raises(NotFound, platform.resume_run(oscar, mine.run_id))
    assert (await platform.runs(olive)).total == 1
    assert (await platform.runs(admin)).total == 1


async def test_workspace_visibility_shares_reading_not_control(
    platform: Platform, admin: Principal
) -> None:
    config = platform.ws.config
    shared = config.model_copy(update={"server": ServerConfig(run_visibility="workspace")})
    await platform.update_config(admin, shared.model_dump(mode="json"))
    await add_agent(platform, admin)
    olive = await add_principal(platform, admin, "olive")
    oscar = await add_principal(platform, admin, "oscar")
    mine = await run_to_rest(platform, olive, "write a.txt: x")

    assert (await platform.run(oscar, mine.run_id)).summary.created_by == "olive"
    await _raises(Forbidden, platform.cancel_run(oscar, mine.run_id, CancelInput()))


async def test_approving_needs_the_permission_and_access_to_the_run(
    platform: Platform, admin: Principal
) -> None:
    await add_agent(platform, admin)
    olive = await add_principal(platform, admin, "olive")  # cannot approve
    ada = await add_principal(platform, admin, "ada", can_approve=True)
    waiting = await run_to_rest(platform, olive, "write a.txt: x")
    [approval] = await platform.approvals(olive)
    approve = ApprovalDecisionInput(approved=True)

    await _raises(
        Forbidden, platform.decide(olive, waiting.run_id, approval.invocation_id, approve)
    )
    # ada may approve, but olive's run is not visible to her: the identifier is not enough.
    assert await platform.approvals(ada) == []
    await _raises(NotFound, platform.decide(ada, waiting.run_id, approval.invocation_id, approve))
    assert not (platform.ws.root / "a.txt").exists()

    await platform.decide(admin, waiting.run_id, approval.invocation_id, approve)
    assert (
        await platform.wait_run(admin, waiting.run_id, timeout_s=10)
    ).phase is RunPhase.COMPLETED


async def test_the_approver_identity_is_the_authenticated_principal(
    platform: Platform, admin: Principal
) -> None:
    config = platform.ws.config
    shared = config.model_copy(update={"server": ServerConfig(run_visibility="workspace")})
    await platform.update_config(admin, shared.model_dump(mode="json"))
    await add_agent(platform, admin)
    olive = await add_principal(platform, admin, "olive")
    ada = await add_principal(platform, admin, "ada", can_approve=True)
    waiting = await run_to_rest(platform, olive, "write a.txt: x")
    [approval] = await platform.approvals(ada)

    await platform.decide(
        ada, waiting.run_id, approval.invocation_id, ApprovalDecisionInput(approved=True)
    )
    await platform.wait_run(ada, waiting.run_id, timeout_s=10)

    [call] = (await platform.run(ada, waiting.run_id)).turns[0].tool_calls
    assert call.approver == "ada"
    assert any(
        e["action"] == "approval.approve" and e["principal"] == "ada"
        for e in await platform.audit(admin)
    )


async def test_run_access_is_the_agent_grant_intersected_with_the_caller(
    platform: Platform, admin: Principal
) -> None:
    await add_agent(
        platform,
        admin,
        memory=MemoryAccess(read=("team:a", "team:b"), write=("team:a", "team:b")),
        knowledge=KnowledgeAccess(collections=("docs", "hr"), labels=("team:docs", "team:hr")),
    )
    olive = await add_principal(
        platform,
        admin,
        "olive",
        memory=MemoryAccess(read=("team:a",), write=()),
        knowledge=KnowledgeAccess(collections=("docs",), labels=("team:docs",)),
    )

    done = await run_to_rest(platform, olive, "hello")
    detail = await platform.run(olive, done.run_id)
    assert detail.memory_access == MemoryAccess(read=("team:a",), write=())
    assert detail.knowledge_access == KnowledgeAccess(collections=("docs",), labels=("team:docs",))

    by_admin = await platform.run(admin, (await run_to_rest(platform, admin, "hello")).run_id)
    assert by_admin.memory_access is not None
    assert by_admin.memory_access.write == (
        "team:a",
        "team:b",
    )


async def test_a_caller_without_grants_runs_without_memory_or_knowledge(
    platform: Platform, admin: Principal
) -> None:
    await add_agent(
        platform,
        admin,
        memory=MemoryAccess(read=("team:a",), write=("team:a",)),
        knowledge=KnowledgeAccess(collections=("docs",)),
    )
    olive = await add_principal(platform, admin, "olive")

    detail = await platform.run(olive, (await run_to_rest(platform, olive, "hello")).run_id)
    assert detail.memory_access is None
    assert detail.knowledge_access is None


async def test_memory_views_and_writes_follow_grants(platform: Platform, admin: Principal) -> None:
    await platform.remember(admin, RememberInput(scope="team:a", content="Alpha uses Postgres."))
    await platform.remember(admin, RememberInput(scope="team:b", content="Beta uses MySQL."))
    olive = await add_principal(
        platform, admin, "olive", memory=MemoryAccess(read=("team:a", "team:b"), write=("team:a",))
    )
    oscar = await add_principal(platform, admin, "oscar")

    assert {m.scope for m in await platform.memories(olive)} == {"team:a", "team:b"}
    assert await platform.memories(oscar) == []
    beta = next(m for m in await platform.memories(olive) if m.scope == "team:b")
    await _raises(NotFound, platform.memory(oscar, beta.memory_id))
    await _raises(
        Forbidden, platform.remember(olive, RememberInput(scope="team:b", content="Beta moved."))
    )
    from hollis.platform.schemas import ForgetInput

    await _raises(Forbidden, platform.forget_memory(olive, beta.memory_id, ForgetInput(reason="x")))
    written = await platform.remember(olive, RememberInput(scope="team:a", content="Alpha moved."))
    assert written.action in ("created", "superseded", "conflict")


async def test_knowledge_views_and_search_follow_grants(
    platform: Platform, admin: Principal
) -> None:
    data = write_readme(platform.ws.root)
    await platform.create_collection(admin, CollectionInput(name="docs"))
    await platform.create_collection(admin, CollectionInput(name="hr"))
    await platform.ingest(admin, "docs", key="public.md", data=data)
    await platform.ingest(admin, "docs", key="ops.md", data=data, labels=("team:ops",))
    await platform.ingest(admin, "hr", key="salaries.md", data=b"# Salaries\n\nConfidential.\n")
    olive = await add_principal(
        platform, admin, "olive", Role.VIEWER, knowledge=KnowledgeAccess(collections=("docs",))
    )

    assert [c.name for c in await platform.collections(olive)] == ["docs"]
    assert [s.key for s in await platform.sources(olive, "docs")] == ["public.md"]
    await _raises(NotFound, platform.sources(olive, "hr"))
    hits = (await platform.search(olive, SearchInput(query="deploy command make ship"))).hits
    assert hits
    assert {h.source_key for h in hits} == {"public.md"}
    ops = next(s for s in await platform.sources(admin, "docs") if s.key == "ops.md")
    await _raises(NotFound, platform.source(olive, "docs", ops.source_id))


async def test_tokens_are_managed_by_their_owner_or_an_admin(
    platform: Platform, admin: Principal
) -> None:
    olive = await add_principal(platform, admin, "olive")
    await add_principal(platform, admin, "oscar")

    own = await platform.create_token(olive, TokenInput(principal="olive", label="laptop"))
    await _raises(Forbidden, platform.create_token(olive, TokenInput(principal="oscar")))
    assert {t.principal for t in await platform.tokens(olive)} == {"olive"}
    others = [t for t in await platform.tokens(admin) if t.principal == "oscar"]
    await _raises(NotFound, platform.revoke_token(olive, others[0].token_id))

    await platform.revoke_token(olive, own.token_id)
    await _raises(PlatformError, platform.authenticate(own.token))
