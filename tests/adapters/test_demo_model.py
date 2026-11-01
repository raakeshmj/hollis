"""The built-in demo model: deterministic rules, always labelled, never mistaken for a model."""

import pytest

from hollis.adapters.demo_model import DemoModelProvider, demo_descriptor
from hollis.runtime.conversation import (
    AssistantTurn,
    ContextBlock,
    ContextKind,
    ToolCall,
    ToolResultMessage,
    ToolResultsTurn,
    ToolSpec,
)
from hollis.runtime.model import ModelRequest, StopReason

ALL_TOOLS = (
    "fs_write_file",
    "fs_read_file",
    "fs_list_dir",
    "memory_remember",
    "memory_recall",
    "knowledge_search",
)


def _request(task: str, *, tools: tuple[str, ...] = ALL_TOOLS, **extra: object) -> ModelRequest:
    specs = tuple(ToolSpec(name=n, description=n, input_schema={"type": "object"}) for n in tools)
    return ModelRequest.model_validate(
        {
            "operation_id": "run/0",
            "model": "hollis-demo-rules-1",
            "input": task,
            "tools": specs,
            "max_output_tokens": 256,
            "timeout_s": 5,
            **extra,
        }
    )


@pytest.mark.parametrize(
    ("task", "tool", "arguments"),
    [
        (
            "write notes.txt: hello there",
            "fs_write_file",
            {"path": "notes.txt", "content": "hello there"},
        ),
        ("read README.md", "fs_read_file", {"path": "README.md"}),
        ("please list the files", "fs_list_dir", {"path": "."}),
        (
            "remember that deploys need the VPN",
            "memory_remember",
            {"content": "deploys need the VPN", "kind": "fact"},
        ),
        ("what do you remember about deploys?", "memory_recall", {"query": "deploys"}),
        ("search rollback steps", "knowledge_search", {"query": "rollback steps"}),
        ("How do I roll back?", "knowledge_search", {"query": "How do I roll back?"}),
    ],
)
async def test_first_turn_rules_call_the_matching_tool(
    task: str, tool: str, arguments: dict[str, str]
) -> None:
    response = await DemoModelProvider().generate(_request(task))
    assert response.stop_reason is StopReason.TOOL_USE
    [call] = response.tool_calls
    assert (call.name, call.arguments) == (tool, arguments)
    assert response.content.startswith("[demo]")


async def test_a_rule_needs_its_tool_to_be_offered() -> None:
    response = await DemoModelProvider().generate(
        _request("write a.txt: x", tools=("fs_read_file",))
    )
    assert response.tool_calls == ()
    assert response.stop_reason is StopReason.END_TURN
    assert "demo model" in response.content


async def test_tool_results_are_reported_with_knowledge_handles() -> None:
    content = (
        "Passages from indexed documents. Cite passages you rely on by their handle, "
        "e.g. [C-1a2b3c4d].\n"
        "<knowledge-results>\n"
        "[C-0000beef] Handbook | collection docs\n"
        "Roll back with `make rollback`. Then tell the team.\n"
        "[C-0000cafe] Handbook | collection docs\nThe on-call engineer approves rollbacks.\n"
        "</knowledge-results>"
    )
    history = (
        AssistantTurn(
            content="", tool_calls=(ToolCall(call_id="c1", name="knowledge_search", arguments={}),)
        ),
        ToolResultsTurn(
            results=(ToolResultMessage(call_id="c1", name="knowledge_search", content=content),)
        ),
    )
    response = await DemoModelProvider().generate(_request("How do I roll back?", history=history))

    assert response.stop_reason is StopReason.END_TURN
    assert response.content.splitlines() == [
        "[demo] From the tools:",
        "- Roll back with `make rollback` [C-0000beef].",
        "- The on-call engineer approves rollbacks [C-0000cafe].",
    ]
    assert "C-1a2b3c4d" not in response.content, "the example handle in the notice is not a passage"


async def test_failed_tools_are_reported_as_failed() -> None:
    history = (
        AssistantTurn(
            content="", tool_calls=(ToolCall(call_id="c1", name="fs_read_file", arguments={}),)
        ),
        ToolResultsTurn(
            results=(
                ToolResultMessage(
                    call_id="c1", name="fs_read_file", content="no such file", is_error=True
                ),
            )
        ),
    )
    response = await DemoModelProvider().generate(_request("read x", history=history))
    assert "- fs_read_file failed: no such file" in response.content


async def test_knowledge_in_context_is_quoted_with_citation_keys() -> None:
    block = ContextBlock(
        kind=ContextKind.KNOWLEDGE,
        source="knowledge:S1",
        text="Handbook\nDeploy with `make ship`. It needs the VPN.",
    )
    response = await DemoModelProvider().generate(
        _request("Tell me about deploys", tools=(), context=(block,))
    )
    assert response.content.splitlines()[1] == "- Deploy with `make ship` [S1]."


async def test_responses_are_deterministic_and_usage_is_estimated() -> None:
    provider = DemoModelProvider()
    first = await provider.generate(_request("hello"))
    second = await provider.generate(_request("hello"))
    assert first == second
    assert first.usage is not None
    assert first.usage.input_tokens > 0
    descriptor = demo_descriptor()
    assert descriptor.deduplicates_requests
    assert descriptor.protocol == "hollis-demo"
