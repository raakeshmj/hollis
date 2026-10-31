"""Python function tools (ADR-0015).

    @tool(effect=ToolEffect.READ_ONLY)
    async def get_weather(city: str, units: Literal["c", "f"] = "c") -> Weather:
        \"\"\"Current weather for a city.\"\"\"

One Pydantic model is derived from the signature. It is the JSON Schema shown to the model AND the
validator applied to the model's arguments, so the contract cannot drift. The return annotation,
when present, validates the tool's output before it is recorded.

Function tools run inside the Hollis process (isolation `none`): they are trusted extensions, not
sandboxed code. Synchronous functions run in a worker thread, which cannot be interrupted on
timeout; prefer `async def` for anything that may block.
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast, get_type_hints, overload

from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, ValidationError, create_model

from hollis.runtime.tools import (
    Isolation,
    JsonPart,
    TextPart,
    ToolArgumentError,
    ToolContext,
    ToolDescriptor,
    ToolEffect,
    ToolError,
    ToolOutput,
    ToolTrust,
)

type ToolFn = Callable[..., Any]
type Reconciler = Callable[[ToolContext], Awaitable[ToolOutput | None]]


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FunctionTool:
    def __init__(
        self,
        fn: ToolFn,
        *,
        name: str | None = None,
        description: str | None = None,
        effect: ToolEffect,
        idempotent: bool | None = None,
        reconcile: Reconciler | None = None,
        timeout_s: float = 30.0,
        version: str = "1",
    ) -> None:
        self._fn = fn
        self._reconcile = reconcile
        hints = get_type_hints(fn, include_extras=True)
        self._context_param: str | None = None
        fields: dict[str, Any] = {}
        for param in inspect.signature(fn).parameters.values():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                raise TypeError(f"{fn.__name__}: *args/**kwargs are not supported in tools")
            annotation = hints.get(param.name)
            if annotation is ToolContext:
                self._context_param = param.name
                continue
            if annotation is None:
                raise TypeError(f"{fn.__name__}: parameter {param.name!r} needs a type annotation")
            default = ... if param.default is inspect.Parameter.empty else param.default
            fields[param.name] = (annotation, default)
        self._arguments = create_model(f"{fn.__name__}_arguments", __base__=_Arguments, **fields)
        self._output: TypeAdapter[Any] | None = (
            TypeAdapter(hints["return"]) if hints.get("return") not in (None, type(None)) else None
        )
        text = description or inspect.getdoc(fn) or ""
        summary = text.strip().split("\n\n")[0].strip()
        if not summary:
            raise TypeError(f"{fn.__name__}: a tool needs a description (docstring)")
        self._descriptor = ToolDescriptor(
            name=name or fn.__name__,
            version=version,
            description=summary,
            input_schema=cast(dict[str, JsonValue], self._arguments.model_json_schema()),
            effect=effect,
            idempotent=effect is ToolEffect.READ_ONLY if idempotent is None else idempotent,
            reconcilable=reconcile is not None,
            trust=ToolTrust.TRUSTED,
            timeout_s=timeout_s,
            isolation=Isolation.NONE,
        )

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._descriptor

    def validate(self, arguments: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        try:
            model = self._arguments.model_validate(dict(arguments))
        except ValidationError as exc:
            raise ToolArgumentError(_describe(exc)) from None
        return cast(dict[str, JsonValue], model.model_dump(mode="json"))

    async def invoke(self, arguments: dict[str, JsonValue], context: ToolContext) -> ToolOutput:
        values = dict(self._arguments.model_validate(arguments))
        if self._context_param is not None:
            values[self._context_param] = context
        if inspect.iscoroutinefunction(self._fn):
            result = await self._fn(**values)
        else:
            result = await asyncio.to_thread(self._fn, **values)
        return self._to_output(result)

    async def reconcile(self, context: ToolContext) -> ToolOutput | None:
        return await self._reconcile(context) if self._reconcile is not None else None

    def _to_output(self, result: Any) -> ToolOutput:
        if isinstance(result, ToolOutput):
            return result
        if self._output is not None:
            try:
                result = self._output.validate_python(result)
            except ValidationError as exc:
                raise ToolError(f"tool returned an invalid result: {_describe(exc)}") from None
        if result is None:
            return ToolOutput(parts=(TextPart(text="ok"),))
        if isinstance(result, str):
            return ToolOutput.text(result)
        adapter: TypeAdapter[Any] = self._output or TypeAdapter(type(cast(object, result)))
        data = cast(JsonValue, adapter.dump_python(result, mode="json"))
        return ToolOutput(parts=(JsonPart(data=data),))


def _describe(exc: ValidationError) -> str:
    """A model-actionable summary of validation errors, without echoing input values."""
    problems = [
        f"{'.'.join(str(p) for p in error['loc']) or '(root)'}: {error['msg']}"
        for error in exc.errors(include_input=False, include_url=False)
    ]
    return "; ".join(problems[:10])


@overload
def tool(fn: ToolFn, /) -> FunctionTool: ...


@overload
def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    effect: ToolEffect = ToolEffect.READ_ONLY,
    idempotent: bool | None = None,
    reconcile: Reconciler | None = None,
    timeout_s: float = 30.0,
    version: str = "1",
) -> Callable[[ToolFn], FunctionTool]: ...


def tool(
    fn: ToolFn | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    effect: ToolEffect = ToolEffect.READ_ONLY,
    idempotent: bool | None = None,
    reconcile: Reconciler | None = None,
    timeout_s: float = 30.0,
    version: str = "1",
) -> FunctionTool | Callable[[ToolFn], FunctionTool]:
    """Turn a typed Python function into a tool. `effect` is a security declaration: choose
    MUTATING or DESTRUCTIVE for anything that changes external state."""

    def build(f: ToolFn) -> FunctionTool:
        return FunctionTool(
            f,
            name=name,
            description=description,
            effect=effect,
            idempotent=idempotent,
            reconcile=reconcile,
            timeout_s=timeout_s,
            version=version,
        )

    return build(fn) if fn is not None else build
