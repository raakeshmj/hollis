"""Tool calling through both protocol adapters: requests, responses and full multi-turn runs."""

from typing import Any

import httpx2
import pytest

from hollis.adapters.anthropic_messages import request_messages
from hollis.adapters.memory_store import InMemoryRunStore
from hollis.runtime.conversation import (
    AssistantTurn,
    ProviderContinuation,
    ToolCall,
    ToolResultMessage,
    ToolResultsTurn,
    ToolSpec,
)
from hollis.runtime.model import (
    FailureKind,
    ModelCallError,
    ModelCapabilities,
    ModelRequest,
    StopReason,
    TextCapabilities,
)
from hollis.runtime.policy import RunBudget
from hollis.runtime.runner import Runner, RunSpec
from hollis.runtime.state import RunStatus
from hollis.tools import ToolRegistry, tool
from tests.support import anthropic, openai_chat

SPEC = ToolSpec(
    name="get_weather",
    description="Weather",
    input_schema={"type": "object", "properties": {"city": {"type": "string"}}},
)
CALL = ToolCall(
    call_id="id1", name="get_weather", arguments={"city": "Oslo"}, raw_arguments='{"city": "Oslo"}'
)
RESULT = ToolResultsTurn(
    results=(ToolResultMessage(call_id="id1", name="get_weather", content="12C"),)
)


def _request(*history: Any, tools: tuple[ToolSpec, ...] = (SPEC,)) -> ModelRequest:
    return ModelRequest(
        operation_id="op",
        model="m",
        input="weather?",
        history=history,
        tools=tools,
        max_output_tokens=256,
        timeout_s=30,
    )


# --- Anthropic Messages -----------------------------------------------------------------------


async def test_anthropic_request_carries_tools_history_and_results() -> None:
    mock = anthropic.MockAnthropic(anthropic.ok())
    error_result = ToolResultsTurn(
        results=(
            ToolResultMessage(call_id="id1", name="get_weather", content="denied", is_error=True),
        )
    )
    await mock.provider().generate(
        _request(AssistantTurn(content="checking", tool_calls=(CALL,)), error_result)
    )
    body = mock.body()
    assert body["tools"] == [
        {"name": "get_weather", "description": "Weather", "input_schema": SPEC.input_schema}
    ]
    assert body["messages"] == [
        {"role": "user", "content": "weather?"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "checking"},
                {"type": "tool_use", "id": "id1", "name": "get_weather", "input": {"city": "Oslo"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "id1",
                    "content": "denied",
                    "is_error": True,
                },
            ],
        },
    ]


def test_anthropic_continuation_is_echoed_verbatim() -> None:
    blocks: Any = [
        {"type": "thinking", "thinking": "", "signature": "SIG"},
        {"type": "tool_use", "id": "id1", "name": "get_weather", "input": {}},
    ]
    turn = AssistantTurn(
        content="",
        tool_calls=(CALL,),
        continuation=ProviderContinuation(protocol="anthropic-messages", data=blocks),
    )
    assert request_messages(_request(turn, RESULT))[1] == {"role": "assistant", "content": blocks}


async def test_anthropic_tool_use_response_is_normalized_with_a_continuation() -> None:
    mock = anthropic.MockAnthropic(anthropic.ok(anthropic.fixture("message_tool_use.json")))
    response = await mock.provider().generate(_request())
    assert response.stop_reason is StopReason.TOOL_USE
    assert response.content == "Let me check."
    assert [(c.call_id, c.name, c.arguments) for c in response.tool_calls] == [
        ("toolu_01A09q90qw90lq917835lq9", "get_weather", {"city": "Oslo"}),
        ("toolu_01B09q90qw90lq917835lq9", "get_time", {"timezone": "Europe/Oslo"}),
    ]
    assert response.omitted_block_types == ()
    assert response.continuation is not None
    data = response.continuation.data
    assert isinstance(data, list)
    assert data[0] == {
        "type": "thinking",
        "thinking": "",
        "signature": "EqQBCgIYAhIMSIGNATUREmustBeEchoed",
    }


async def test_anthropic_requests_without_tools_send_no_tools_field() -> None:
    mock = anthropic.MockAnthropic(anthropic.ok())
    await mock.provider().generate(_request(tools=()))
    assert "tools" not in mock.body()


# --- OpenAI Chat Completions --------------------------------------------------------------------


async def test_openai_request_carries_tools_history_and_results() -> None:
    mock = openai_chat.MockChatServer(openai_chat.ok())
    error_result = ToolResultsTurn(
        results=(
            ToolResultMessage(call_id="id1", name="get_weather", content="denied", is_error=True),
        )
    )
    await mock.provider().generate(
        _request(AssistantTurn(content="", tool_calls=(CALL,)), error_result)
    )
    body = mock.body()
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Weather",
                "parameters": SPEC.input_schema,
            },
        }
    ]
    assert body["messages"][1:] == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "id1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Oslo"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "id1", "content": "[tool error] denied"},
    ]


async def test_openai_tool_calls_are_normalized() -> None:
    mock = openai_chat.MockChatServer(openai_chat.ok("tool_calls.json"))
    response = await mock.provider().generate(_request())
    assert response.stop_reason is StopReason.TOOL_USE
    assert [(c.call_id, c.name, c.arguments, c.raw_arguments) for c in response.tool_calls] == [
        ("call_abc", "get_weather", {"city": "Oslo"}, '{"city": "Oslo"}'),
        ("call_def", "get_time", {"timezone": "Europe/Oslo"}, '{"timezone":"Europe/Oslo"}'),
    ]


async def test_openai_unparseable_arguments_are_kept_for_the_runtime_to_reject() -> None:
    body = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [
                        {
                            "id": "c",
                            "type": "function",
                            "function": {"name": "f", "arguments": "{bad json"},
                        }
                    ]
                },
            }
        ]
    }
    response = (
        await openai_chat.MockChatServer(httpx2.Response(200, json=body))
        .provider()
        .generate(_request())
    )
    (call,) = response.tool_calls
    assert (call.arguments, call.raw_arguments) == (None, "{bad json")
    assert call.parse_error is not None


@pytest.mark.parametrize(
    "message",
    [
        {"function_call": {"name": "f", "arguments": "{}"}},
        {"tool_calls": [{"id": "c", "type": "custom", "custom": {"name": "f", "input": "x"}}]},
    ],
    ids=["legacy-function-call", "custom-tool-type"],
)
async def test_unsupported_tool_call_shapes_are_never_silently_dropped(
    message: dict[str, Any],
) -> None:
    body = {"choices": [{"finish_reason": "tool_calls", "message": message}]}
    with pytest.raises(ModelCallError) as exc_info:
        await (
            openai_chat.MockChatServer(httpx2.Response(200, json=body))
            .provider()
            .generate(_request())
        )
    assert exc_info.value.kind is FailureKind.INVALID_RESPONSE


# --- full runs through each adapter ---------------------------------------------------------


def _registry() -> tuple[ToolRegistry, list[str]]:
    seen: list[str] = []
    registry = ToolRegistry()

    @registry.add
    @tool
    async def get_weather(city: str) -> str:
        """Current weather for a city."""
        seen.append(city)
        return f"{city}: 12C"

    @registry.add
    @tool
    async def get_time(timezone: str) -> str:
        """Current time."""
        return f"{timezone}: 10:00"

    return registry, seen


async def test_multi_turn_run_through_the_anthropic_adapter() -> None:
    mock = anthropic.MockAnthropic(
        anthropic.ok(anthropic.fixture("message_tool_use.json")), anthropic.ok()
    )
    registry, seen = _registry()
    runner = Runner(InMemoryRunStore(), mock.provider(), tools=registry)
    run = await runner.create(
        RunSpec(
            model="claude",
            input="weather?",
            tools=("get_weather", "get_time"),
            budget=RunBudget(max_output_tokens=256),
        )
    )
    final = await runner.execute(run.run_id)

    assert final.status is RunStatus.COMPLETED
    assert seen == ["Oslo"]
    second = mock.body(1)["messages"]
    assert second[1]["content"][0]["signature"] == "EqQBCgIYAhIMSIGNATUREmustBeEchoed"  # echoed
    assert [r["tool_use_id"] for r in second[2]["content"]] == [
        "toolu_01A09q90qw90lq917835lq9",
        "toolu_01B09q90qw90lq917835lq9",
    ]
    assert second[2]["content"][0]["content"] == "Oslo: 12C"


async def test_multi_turn_run_through_the_openai_adapter() -> None:
    mock = openai_chat.MockChatServer(openai_chat.ok("tool_calls.json"), openai_chat.ok())
    registry, seen = _registry()
    descriptor = openai_chat.DESCRIPTOR.model_copy(
        update={"capabilities": ModelCapabilities(text=TextCapabilities(tools=True))}
    )
    runner = Runner(InMemoryRunStore(), mock.provider(descriptor=descriptor), tools=registry)
    run = await runner.create(
        RunSpec(
            model="local-qwen",
            input="weather?",
            tools=("get_weather", "get_time"),
            budget=RunBudget(max_output_tokens=256),
        )
    )
    final = await runner.execute(run.run_id)

    assert final.status is RunStatus.COMPLETED
    assert seen == ["Oslo"]
    tool_messages = [m for m in mock.body(1)["messages"] if m["role"] == "tool"]
    assert [(m["tool_call_id"], m["content"]) for m in tool_messages] == [
        ("call_abc", "Oslo: 12C"),
        ("call_def", "Europe/Oslo: 10:00"),
    ]
