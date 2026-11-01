"""End-to-end journeys through the Platform API with the deterministic demo model: the same
service layer the REST API, SDK and CLI use. No network, no credentials."""

from pathlib import Path

from hollis.platform.auth import Principal
from hollis.platform.schemas import (
    ApprovalDecisionInput,
    CollectionInput,
    RunPhase,
    SearchInput,
)
from hollis.platform.services import Platform
from hollis.runtime.knowledge import KnowledgeAccess
from hollis.runtime.memory import MemoryAccess
from tests.support.platform import add_agent, run_to_rest, write_readme

KNOWLEDGE = KnowledgeAccess(collections=("docs",), labels=("team:docs",))
MEMORY = MemoryAccess(read=("project:demo",), write=("project:demo",))


async def _with_handbook(platform: Platform, admin: Principal) -> None:
    await platform.create_collection(admin, CollectionInput(name="docs", description="handbook"))
    data = write_readme(platform.ws.root)
    result = await platform.ingest(
        admin, "docs", key="HANDBOOK.md", data=data, labels=("team:docs",)
    )
    assert result.action == "created"


async def test_write_waits_for_approval_and_runs_once_approved(
    platform: Platform, admin: Principal
) -> None:
    await add_agent(platform, admin)
    waiting = await run_to_rest(platform, admin, "write notes.txt: hello from hollis")
    assert waiting.phase is RunPhase.WAITING_APPROVAL
    assert not (platform.ws.root / "notes.txt").exists()

    [approval] = await platform.approvals(admin)
    assert approval.tool == "fs_write_file"
    assert approval.arguments == {"path": "notes.txt", "content": "hello from hollis"}
    await platform.decide(
        admin, approval.run_id, approval.invocation_id, ApprovalDecisionInput(approved=True)
    )
    done = await platform.wait_run(admin, approval.run_id, timeout_s=10)

    assert done.phase is RunPhase.COMPLETED
    assert (platform.ws.root / "notes.txt").read_text() == "hello from hollis"
    detail = await platform.run(admin, done.run_id)
    assert detail.answer is not None
    assert detail.answer.startswith("[demo]")
    [call] = detail.turns[0].tool_calls
    assert (call.status, call.approved, call.approver) == ("succeeded", True, "admin")
    types = [e.type for e in await platform.run_events(admin, done.run_id)]
    assert types[0] == "run_created"
    assert types[-1] == "run_completed"
    assert "tool_approval_resolved" in types
    assert await platform.approvals(admin) == []
    [activity] = await platform.tool_activity(admin)
    assert (activity.tool, activity.approver, activity.outcome) == (
        "fs_write_file",
        "admin",
        "success",
    )


async def test_denied_write_never_executes(platform: Platform, admin: Principal) -> None:
    await add_agent(platform, admin)
    await run_to_rest(platform, admin, "write notes.txt: never")
    [approval] = await platform.approvals(admin)
    await platform.decide(
        admin,
        approval.run_id,
        approval.invocation_id,
        ApprovalDecisionInput(approved=False, reason="not today"),
    )
    done = await platform.wait_run(admin, approval.run_id, timeout_s=10)

    assert done.phase is RunPhase.COMPLETED
    assert not (platform.ws.root / "notes.txt").exists()
    [call] = (await platform.run(admin, done.run_id)).turns[0].tool_calls
    assert (call.status, call.approved, call.approval_reason) == ("denied", False, "not today")
    assert call.attempts == 0


async def test_question_is_answered_from_knowledge_with_citations(
    platform: Platform, admin: Principal
) -> None:
    await _with_handbook(platform, admin)
    await add_agent(platform, admin, knowledge=KNOWLEDGE)

    done = await run_to_rest(platform, admin, "How do I deploy?")

    assert done.phase is RunPhase.COMPLETED
    detail = await platform.run(admin, done.run_id)
    assert detail.passages, "the run retrieved passages before the first model call"
    assert detail.answer is not None
    assert "make ship" in detail.answer
    assert "[C-" in detail.answer
    assert detail.grounding is not None
    search = await platform.search(admin, SearchInput(query="deploy command"))
    assert search.hits[0].source_key == "HANDBOOK.md"


async def test_session_memory_carries_between_runs_of_a_conversation(
    platform: Platform, admin: Principal
) -> None:
    await add_agent(platform, admin, memory=MEMORY)

    first = await run_to_rest(platform, admin, "remember that the VPN is required", session="s1")
    [approval] = await platform.approvals(admin)
    assert isinstance(approval.arguments, dict)
    assert (approval.arguments["content"], approval.arguments["kind"]) == (
        "the VPN is required",
        "fact",
    )
    await platform.decide(
        admin, first.run_id, approval.invocation_id, ApprovalDecisionInput(approved=True)
    )
    await platform.wait_run(admin, first.run_id, timeout_s=10)
    [memory] = await platform.memories(admin, query="VPN")
    assert memory.scope in ("project:demo", "session:admin/s1")

    second = await run_to_rest(platform, admin, "what do you remember about the VPN?", session="s1")
    detail = await platform.run(admin, second.run_id)
    assert second.phase is RunPhase.COMPLETED
    assert detail.memory_access is not None
    assert "session:admin/s1" in detail.memory_access.read
    assert detail.answer is not None
    assert "VPN" in detail.answer


async def test_runs_list_newest_first_with_summaries(platform: Platform, admin: Principal) -> None:
    await add_agent(platform, admin)
    first = await run_to_rest(platform, admin, "hello")
    second = await run_to_rest(platform, admin, "list files")

    page = await platform.runs(admin)
    assert [r.run_id for r in page.items] == [second.run_id, first.run_id]
    assert page.total == 2
    assert page.items[0].tool_calls == 1
    assert page.items[1].tool_calls == 0
    assert all(r.agent == "helper" and r.created_by == "admin" for r in page.items)
    completed = await platform.runs(admin, phase=RunPhase.COMPLETED)
    assert completed.total == 2


async def test_episode_is_recorded_when_an_agent_asks_for_it(
    platform: Platform, admin: Principal, tmp_path: Path
) -> None:
    await add_agent(platform, admin, memory=MEMORY, record_episodes=True)
    done = await run_to_rest(platform, admin, "list files")
    assert done.phase is RunPhase.COMPLETED
    episodes = [m for m in await platform.memories(admin) if m.kind.value == "episode"]
    assert len(episodes) == 1
    assert episodes[0].scope == "project:demo"
    assert tmp_path == platform.ws.root
