"""Context blocks on the wire, and exact token counting where a protocol offers it."""

import httpx2
import pytest

from hollis.runtime.conversation import CONTEXT_NOTICE, ContextBlock, ContextKind
from hollis.runtime.model import FailureKind, ModelCallError, ModelRequest
from tests.support import anthropic, openai_chat

BLOCKS = (
    ContextBlock(kind=ContextKind.MEMORY, source="memory:m1", text="[preference] metric units"),
    ContextBlock(kind=ContextKind.SUMMARY, source="summary:0", text="Turns 0-3: cert expired."),
)


def _request(context: tuple[ContextBlock, ...] = BLOCKS) -> ModelRequest:
    return ModelRequest(
        operation_id="op",
        model="m",
        instructions="You are a careful assistant.",
        input="What is the root cause?",
        context=context,
        max_output_tokens=100,
        timeout_s=5,
    )


async def test_anthropic_sends_context_as_a_separate_data_block_before_the_input() -> None:
    mock = anthropic.MockAnthropic(anthropic.ok())
    await mock.provider().generate(_request())

    body = mock.body()
    first = body["messages"][0]
    assert first["role"] == "user"
    assert [b["type"] for b in first["content"]] == ["text", "text"]
    context, user = first["content"]
    assert context["text"].startswith(CONTEXT_NOTICE)
    assert 'source="memory:m1"' in context["text"]
    assert "cert expired" in context["text"]
    assert user["text"] == "What is the root cause?"
    assert body["system"] == "You are a careful assistant."  # never mixed into instructions


async def test_anthropic_without_context_is_unchanged() -> None:
    mock = anthropic.MockAnthropic(anthropic.ok())
    await mock.provider().generate(_request(()))
    assert mock.body()["messages"][0]["content"] == "What is the root cause?"


async def test_openai_compatible_prefixes_context_in_the_single_user_message() -> None:
    mock = openai_chat.MockChatServer(openai_chat.ok())
    await mock.provider().generate(_request())

    messages = mock.body()["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == "You are a careful assistant."
    user = messages[1]["content"]
    assert user.startswith(CONTEXT_NOTICE)
    assert user.endswith("\n\nWhat is the root cause?")


async def test_anthropic_counts_input_tokens_with_the_same_request_shape() -> None:
    mock = anthropic.MockAnthropic(httpx2.Response(200, json={"input_tokens": 4321}))
    counted = await mock.provider().count_input_tokens(_request())

    assert counted == 4321
    request = mock.requests[0]
    assert request.url.path == "/v1/messages/count_tokens"
    body = mock.body()
    assert body["system"] == "You are a careful assistant."
    assert body["messages"][0]["content"][1]["text"] == "What is the root cause?"
    assert "max_tokens" not in body


async def test_anthropic_count_errors_are_classified() -> None:
    mock = anthropic.MockAnthropic(anthropic.api_error(529, "overloaded_error"))
    with pytest.raises(ModelCallError) as caught:
        await mock.provider().count_input_tokens(_request())
    assert caught.value.kind is FailureKind.OVERLOADED
