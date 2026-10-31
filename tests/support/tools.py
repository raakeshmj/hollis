"""Minimal Tool implementations for runtime tests (independent of hollis.tools)."""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import JsonValue

from hollis.runtime.conversation import ToolCall, ToolResultMessage, ToolResultsTurn
from hollis.runtime.model import ModelRequest, ModelResponse, StopReason
from hollis.runtime.tools import (
    ToolArgumentError,
    ToolContext,
    ToolDescriptor,
    ToolEffect,
    ToolOutput,
    ToolTrust,
)

type Handler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolOutput]]


async def _echo(arguments: dict[str, Any], context: ToolContext) -> ToolOutput:
    return ToolOutput.text(f"echo {arguments}")


class FakeTool:
    def __init__(
        self,
        name: str,
        *,
        effect: ToolEffect = ToolEffect.READ_ONLY,
        idempotent: bool | None = None,
        reconcilable: bool = False,
        trust: ToolTrust = ToolTrust.TRUSTED,
        timeout_s: float = 5.0,
        version: str = "1",
        required: tuple[str, ...] = (),
        handler: Handler = _echo,
        reconcile_result: ToolOutput | None = None,
    ) -> None:
        self._descriptor = ToolDescriptor(
            name=name,
            version=version,
            description=f"test tool {name}",
            input_schema={
                "type": "object",
                "properties": {k: {"type": "string"} for k in required},
                "required": list(required),
            },
            effect=effect,
            idempotent=effect is ToolEffect.READ_ONLY if idempotent is None else idempotent,
            reconcilable=reconcilable,
            trust=trust,
            timeout_s=timeout_s,
        )
        self._handler = handler
        self._reconcile_result = reconcile_result
        self.calls: list[tuple[dict[str, Any], ToolContext]] = []
        self.reconciled = 0

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._descriptor

    def validate(self, arguments: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        missing = [k for k in self._descriptor.input_schema["required"] if k not in arguments]  # type: ignore[union-attr]  # test schema
        if missing:
            raise ToolArgumentError(f"missing required arguments: {missing}")
        return dict(arguments)

    async def invoke(self, arguments: dict[str, JsonValue], context: ToolContext) -> ToolOutput:
        self.calls.append((dict(arguments), context))
        return await self._handler(dict(arguments), context)

    async def reconcile(self, context: ToolContext) -> ToolOutput | None:
        self.reconciled += 1
        return self._reconcile_result


class Catalog:
    def __init__(self, *tools: FakeTool) -> None:
        self.tools = {t.descriptor.name: t for t in tools}

    def get(self, name: str) -> FakeTool | None:
        return self.tools.get(name)


def call(name: str, call_id: str = "call_1", /, **arguments: JsonValue) -> ToolCall:
    return ToolCall(call_id=call_id, name=name, arguments=dict(arguments))


def wants(*calls: ToolCall, content: str = "") -> ModelResponse:
    return ModelResponse(content=content, tool_calls=calls, stop_reason=StopReason.TOOL_USE)


def answer(text: str = "done") -> ModelResponse:
    return ModelResponse(content=text)


def results(request: ModelRequest, item: int = 1) -> tuple[ToolResultMessage, ...]:
    """The tool results carried in a model request's history (narrowed for type checking)."""
    turn = request.history[item]
    assert isinstance(turn, ToolResultsTurn)
    return turn.results
