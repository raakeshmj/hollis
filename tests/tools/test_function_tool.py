from collections.abc import Callable
from typing import Annotated, Any, Literal
from uuid import uuid4

import pytest
from pydantic import BaseModel, Field

from hollis.runtime.tools import (
    JsonPart,
    TextPart,
    ToolArgumentError,
    ToolContext,
    ToolEffect,
    ToolError,
)
from hollis.tools import FunctionTool, ToolOutput, tool

CONTEXT = ToolContext(run_id=uuid4(), invocation_id="run/turn/0/call/0", attempt=1)


class Weather(BaseModel):
    city: str
    celsius: float


@tool
async def get_weather(
    city: Annotated[str, Field(description="City name", min_length=1)],
    units: Literal["c", "f"] = "c",
) -> Weather:
    """Current weather for a city.

    Longer notes are not part of the model-facing description.
    """
    return Weather(city=city, celsius=21.5 if units == "c" else 70.7)


def test_descriptor_is_derived_from_the_signature() -> None:
    d = get_weather.descriptor
    assert (d.name, d.description, d.version, d.effect) == (
        "get_weather",
        "Current weather for a city.",
        "1",
        ToolEffect.READ_ONLY,
    )
    assert d.idempotent  # read-only defaults to idempotent
    schema = d.input_schema
    assert schema["required"] == ["city"]
    assert schema["additionalProperties"] is False
    props = schema["properties"]
    assert isinstance(props, dict)
    assert props["city"] == {
        "description": "City name",
        "minLength": 1,
        "title": "City",
        "type": "string",
    }
    assert props["units"]["enum"] == ["c", "f"]  # type: ignore[index]


def test_the_model_facing_schema_is_the_validation_contract() -> None:
    assert get_weather.descriptor.spec.input_schema == get_weather.descriptor.input_schema
    assert get_weather.validate({"city": "Oslo"}) == {"city": "Oslo", "units": "c"}
    for bad, fragment in [
        ({}, "city: Field required"),
        ({"city": ""}, "at least 1 character"),
        ({"city": "Oslo", "units": "k"}, "units"),
        ({"city": "Oslo", "rm": "-rf"}, "Extra inputs are not permitted"),
    ]:
        with pytest.raises(ToolArgumentError, match=fragment):
            get_weather.validate(bad)


def test_validation_errors_do_not_echo_input_values() -> None:
    with pytest.raises(ToolArgumentError) as exc_info:
        get_weather.validate({"city": "Oslo", "units": "SECRET-VALUE"})
    assert "SECRET-VALUE" not in str(exc_info.value)


async def test_structured_return_values_become_json_parts() -> None:
    output = await get_weather.invoke({"city": "Oslo", "units": "c"}, CONTEXT)
    assert output.parts == (JsonPart(data={"city": "Oslo", "celsius": 21.5}),)


async def test_sync_functions_context_injection_and_plain_text() -> None:
    seen: list[ToolContext] = []

    @tool(name="whoami", effect=ToolEffect.MUTATING, idempotent=True, timeout_s=5, version="2")
    def whoami(context: ToolContext, prefix: str = "id") -> str:
        """Report the invocation id."""
        seen.append(context)
        return f"{prefix}:{context.invocation_id}"

    assert "context" not in whoami.descriptor.input_schema["properties"]  # type: ignore[operator]
    assert (whoami.descriptor.idempotent, whoami.descriptor.version) == (True, "2")
    output = await whoami.invoke({"prefix": "x"}, CONTEXT)
    assert output.parts == (TextPart(text="x:run/turn/0/call/0"),)
    assert seen == [CONTEXT]


async def test_output_is_validated_against_the_return_annotation() -> None:
    @tool
    async def broken() -> int:
        """Returns the wrong type."""
        return "not an int"  # type: ignore[return-value]  # deliberately wrong

    with pytest.raises(ToolError, match="invalid result"):
        await broken.invoke({}, CONTEXT)


async def test_tool_output_and_none_pass_through() -> None:
    @tool
    async def explicit() -> ToolOutput:
        """Explicit output."""
        return ToolOutput.text("raw", is_error=True)

    @tool
    async def nothing() -> None:
        """Returns nothing."""

    assert (await explicit.invoke({}, CONTEXT)).is_error
    assert (await nothing.invoke({}, CONTEXT)).parts == (TextPart(text="ok"),)


def test_definition_errors_are_caught_early() -> None:
    def undocumented(x: int) -> int:
        return x

    def untyped(x):  # type: ignore[no-untyped-def]  # deliberately untyped
        """Doc."""

    def variadic(*args: int) -> None:
        """Doc."""

    cases: list[tuple[Callable[..., Any], str]] = [
        (undocumented, "description"),
        (untyped, "annotation"),  # pyright: ignore[reportUnknownArgumentType]  # deliberate
        (variadic, r"\*args"),
    ]
    for fn, message in cases:
        with pytest.raises(TypeError, match=message):
            FunctionTool(fn, effect=ToolEffect.READ_ONLY)


async def test_reconcile_hook_marks_the_tool_reconcilable() -> None:
    async def lookup(context: ToolContext) -> ToolOutput | None:
        return ToolOutput.text(f"found {context.invocation_id}")

    @tool(effect=ToolEffect.MUTATING, reconcile=lookup)
    async def charge(amount: int) -> str:
        """Charge a card."""
        return "charged"

    assert charge.descriptor.reconcilable
    assert not charge.descriptor.idempotent
    result = await charge.reconcile(CONTEXT)
    assert result is not None
    assert result.parts[0] == TextPart(text="found run/turn/0/call/0")


def test_digest_changes_with_the_contract() -> None:
    @tool(version="1")
    async def a(x: int) -> int:
        """Doc."""
        return x

    @tool(version="2", name="a")
    async def a2(x: int) -> int:
        """Doc."""
        return x

    assert a.descriptor.schema_digest != a2.descriptor.schema_digest
