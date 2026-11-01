"""Pure orchestration logic: grant narrowing, delegation effects, budgets over a tree, report
parsing and size limits, disagreement detection, and the demo model's team syntax."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from hollis.adapters.demo_model import _team_plan  # pyright: ignore[reportPrivateUsage]
from hollis.platform.orchestration import (
    budget_refusal,
    build_report,
    delegation_effect,
    disagreements,
    narrow_knowledge,
    narrow_memory,
    parse_self_report,
)
from hollis.platform.schemas import Agent
from hollis.runtime.delegation import DelegationLimits, Lineage, report_meta
from hollis.runtime.knowledge import KnowledgeAccess
from hollis.runtime.memory import MemoryAccess
from hollis.runtime.model import ModelResponse, Usage
from hollis.runtime.results import render_parts
from hollis.runtime.state import fold
from hollis.runtime.tools import ToolEffect
from tests.support.events import build_log, created

NOW = datetime(2026, 11, 1, tzinfo=UTC)


def _agent(name: str, tools: tuple[str, ...] = (), delegates: tuple[str, ...] = ()) -> Agent:
    return Agent(
        name=name,
        model="demo",
        tools=tools,
        delegates=delegates,
        created_at=NOW,
        updated_at=NOW,
        created_by="admin",
        version=1,
    )


def test_memory_narrows_and_reports_what_it_removed() -> None:
    configured = MemoryAccess(read=("a:x", "b:y"), write=("a:x", "b:y"))
    narrowed, removed = narrow_memory(configured, MemoryAccess(read=("a:x", "b:y"), write=("a:x",)))
    assert narrowed == MemoryAccess(read=("a:x", "b:y"), write=("a:x",))
    assert removed == ["memory write b:y"]
    assert narrow_memory(configured, None) == (
        None,
        ["memory read a:x", "memory read b:y", "memory write a:x", "memory write b:y"],
    )
    assert narrow_memory(None, configured) == (None, [])


def test_knowledge_narrows_collections_and_labels() -> None:
    configured = KnowledgeAccess(collections=("docs", "hr"), labels=("group:a", "group:b"))
    ceiling = KnowledgeAccess(collections=("docs",), labels=("group:a",))
    narrowed, removed = narrow_knowledge(configured, ceiling)
    assert narrowed == KnowledgeAccess(collections=("docs",), labels=("group:a",))
    assert removed == ["knowledge hr", "label group:b"]


def test_delegation_effect_is_the_strongest_reachable_effect() -> None:
    effects = {
        "read": ToolEffect.READ_ONLY,
        "write": ToolEffect.MUTATING,
        "rm": ToolEffect.DESTRUCTIVE,
    }
    agents = {
        "reader": _agent("reader", ("read",)),
        "writer": _agent("writer", ("write",)),
        "lead": _agent("lead", ("read",), delegates=("reader", "writer")),
        "loop": _agent("loop", ("read",), delegates=("loop",)),
        "mcp": _agent("mcp", ("mcp_server_tool",)),
        "boss": _agent("boss", delegates=("lead",)),
    }
    effect = effects.get
    assert delegation_effect("reader", agents, effect) is ToolEffect.READ_ONLY
    assert delegation_effect("lead", agents, effect) is ToolEffect.MUTATING
    assert delegation_effect("boss", agents, effect) is ToolEffect.MUTATING, "transitive"
    assert delegation_effect("loop", agents, effect) is ToolEffect.READ_ONLY, "cycles terminate"
    assert delegation_effect("mcp", agents, effect) is ToolEffect.MUTATING, "unknown tools"
    assert delegation_effect("ghost", agents, effect) is ToolEffect.MUTATING
    agents["rm"] = _agent("rm", ("rm",))
    assert delegation_effect("rm", agents, effect) is ToolEffect.DESTRUCTIVE


def _row(
    run_id: UUID, parent: UUID | None, depth: int, tokens: int, budget: int | None = None
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "parent_id": parent,
        "depth": depth,
        "data": {"usage": {"input_tokens": tokens, "output_tokens": 0}, "token_budget": budget},
    }


def test_budgets_apply_to_subtrees_along_the_path_to_the_root() -> None:
    root, branch, leaf, other = uuid4(), uuid4(), uuid4(), uuid4()
    tree = [
        _row(root, None, 0, 100, budget=1000),
        _row(branch, root, 1, 100, budget=300),
        _row(leaf, branch, 2, 150),
        _row(other, root, 1, 500),
    ]
    payload = created(
        lineage=Lineage(root_run_id=root, parent_run_id=branch, parent_invocation_id="i", depth=2),
        delegation=DelegationLimits(token_budget=300),
    )
    leaf_state = fold(build_log(leaf, [payload]))
    assert budget_refusal(leaf_state, tree) is None  # branch: 100 + 0 (fresh leaf) < 300
    tree[2]["data"]["usage"]["input_tokens"] = 0
    leaf_state = leaf_state.model_copy(
        update={"usage": leaf_state.usage.model_copy(update={"input_tokens": 200})}
    )
    reason = budget_refusal(leaf_state, tree)
    assert reason == "the token budget of 300 for this branch is used up (300 tokens)"
    tree[3]["data"]["usage"]["input_tokens"] = 650  # root subtree: 0 + 100 + 0 + 650
    root_state = fold(build_log(root, [created(delegation=DelegationLimits(token_budget=1000))]))
    assert budget_refusal(root_state, tree) is None
    share = budget_refusal(root_state, tree, share=0.75)
    assert share is not None
    assert share.startswith("750 of the 1000-token budget for this orchestration are used")


def test_self_reports_are_validated_and_split_from_the_prose() -> None:
    prose, report = parse_self_report(
        'Deploy with make ship [S1].\n\n```json\n{"status": "done", "answer": "make ship", '
        '"confidence": "certain", "open_questions": ["VPN?", 3], "extra": true}\n```'
    )
    assert prose == "Deploy with make ship [S1]."
    assert report == {"status": "done", "answer": "make ship", "open_questions": ["VPN?"]}
    assert parse_self_report("no block") == ("no block", {})
    assert parse_self_report("bad ```json\n{oops}\n```")[1] == {}


def test_reports_fit_the_parents_tool_result_limit() -> None:
    run_id = uuid4()
    op = f"{run_id}/model/0"
    from hollis.runtime.events import ModelCallStarted, ModelResponseRecorded, RunCompleted

    long_answer = "word " * 5000
    payloads = [
        created(
            lineage=Lineage(
                root_run_id=uuid4(), parent_run_id=uuid4(), parent_invocation_id="i", depth=2
            )
        ),
        ModelCallStarted(operation_id=op, attempt=1),
        ModelResponseRecorded(
            operation_id=op,
            attempt=1,
            response=ModelResponse(
                content=long_answer, usage=Usage(input_tokens=5, output_tokens=7)
            ),
        ),
        RunCompleted(),
    ]
    state = fold(build_log(run_id, payloads))
    output = build_report(state, "writer", limit=4000)
    text = render_parts(output.parts)
    assert len(text) <= 4000
    assert "[answer truncated]" in text
    meta = report_meta(text)
    assert meta is not None
    assert meta["outcome"] == "completed"
    assert meta["usage"] == {"input_tokens": 5, "output_tokens": 7, "turns": 1, "tool_calls": 0}
    assert not output.is_error


def test_disagreement_needs_the_same_objective_and_different_answers() -> None:
    a, b, c = uuid4(), uuid4(), uuid4()
    assert disagreements([("Refund window?", a, "14 days."), ("refund window", b, "14 Days")]) == []
    [found] = disagreements(
        [("Refund window?", a, "14 days"), ("refund window", b, "30 days"), ("Other?", c, "x")]
    )
    assert found.runs == (a, b)


def test_the_demo_team_syntax() -> None:
    tools = {"delegate_researcher", "delegate_analyst", "delegate_reviewer"}
    plan = _team_plan(
        "@researcher find the steps and @analyst check costs, then @reviewer compare them. "
        "@nobody ignored",
        tools,
    )
    assert plan == [
        (0, 0, "researcher", "find the steps"),
        (1, 0, "analyst", "check costs"),
        (2, 1, "reviewer", "compare them. @nobody ignored"),
    ]
    assert _team_plan("no mentions here", tools) == []
