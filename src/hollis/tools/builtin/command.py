"""Fixed-command tools run as a subprocess (ADR-0016). NOT A SANDBOX.

A CommandTool runs one developer-chosen program with an argv template. Model-supplied values fill
whole argv elements; no shell is ever involved, so shell metacharacters stay literal. Values
beginning with "-" are refused unless a parameter explicitly allows them (option injection, e.g.
`--upload-pack=` on git). The child gets a scrubbed environment, no stdin, its own process group,
POSIX resource limits where available, a hard timeout that kills the whole group, and an output
cap that kills the process once exceeded.

What this does NOT provide: filesystem, network or privilege isolation. The child runs as the
same user with the same access as Hollis. Its isolation level is `process`, never `sandbox`.
There is no generic "run any shell command" tool in Hollis.
"""

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, JsonValue, ValidationError

from hollis.runtime.tools import (
    Isolation,
    ToolArgumentError,
    ToolContext,
    ToolDescriptor,
    ToolEffect,
    ToolOutput,
    ToolTrust,
)

DEFAULT_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}


class CommandTool:
    def __init__(
        self,
        name: str,
        argv: Sequence[str],
        *,
        description: str,
        parameters: type[BaseModel],
        effect: ToolEffect,
        idempotent: bool | None = None,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        timeout_s: float = 30.0,
        max_output_bytes: int = 64_000,
        allow_leading_dash: frozenset[str] = frozenset(),
        cpu_seconds: int | None = None,
        max_file_bytes: int | None = 64 * 1024 * 1024,
        max_open_files: int | None = 256,
        version: str = "1",
    ) -> None:
        if not argv or "{" in argv[0]:
            raise ValueError("argv[0] must be a fixed program, not a placeholder")
        if not Path(argv[0]).is_absolute():
            raise ValueError("argv[0] must be an absolute path (no PATH lookup)")
        fields = set(parameters.model_fields)
        for element in argv[1:]:
            if "{" in element and not (
                element.startswith("{") and element.endswith("}") and element[1:-1] in fields
            ):
                raise ValueError(
                    f"argv element {element!r} must be a literal or a whole '{{param}}'"
                )
        if parameters.model_config.get("extra") != "forbid":
            raise ValueError("parameters model must set extra='forbid'")
        self._argv = tuple(argv)
        self._parameters = parameters
        self._cwd = Path(cwd) if cwd is not None else None
        self._env = dict(DEFAULT_ENV if env is None else env)
        self._max_output = max_output_bytes
        self._allow_dash = allow_leading_dash
        self._cpu = cpu_seconds if cpu_seconds is not None else int(timeout_s) + 1
        self._fsize = max_file_bytes
        self._nofile = max_open_files
        self._descriptor = ToolDescriptor(
            name=name,
            version=version,
            description=description,
            input_schema=cast(dict[str, JsonValue], parameters.model_json_schema()),
            effect=effect,
            idempotent=effect is ToolEffect.READ_ONLY if idempotent is None else idempotent,
            trust=ToolTrust.TRUSTED,
            timeout_s=timeout_s,
            isolation=Isolation.PROCESS,
        )

    @property
    def descriptor(self) -> ToolDescriptor:
        return self._descriptor

    def validate(self, arguments: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        try:
            values = self._parameters.model_validate(dict(arguments)).model_dump(mode="json")
        except ValidationError as exc:
            problems = [
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                for e in exc.errors(include_input=False, include_url=False)
            ]
            raise ToolArgumentError("; ".join(problems)) from None
        for key, value in values.items():
            if isinstance(value, str):
                if value.startswith("-") and key not in self._allow_dash:
                    raise ToolArgumentError(f"{key}: values may not start with '-'")
                if "\x00" in value:
                    raise ToolArgumentError(f"{key}: NUL bytes are not allowed")
        return cast(dict[str, JsonValue], values)

    def argv_for(self, arguments: Mapping[str, Any]) -> list[str]:
        argv = [self._argv[0]]
        for element in self._argv[1:]:
            if element.startswith("{") and element.endswith("}"):
                argv.append(str(arguments[element[1:-1]]))
            else:
                argv.append(element)
        return argv

    async def invoke(self, arguments: dict[str, JsonValue], context: ToolContext) -> ToolOutput:
        process = await asyncio.create_subprocess_exec(
            *self.argv_for(self.validate(arguments)),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self._cwd,
            env=self._env,
            start_new_session=True,
            preexec_fn=self._limits if sys.platform != "win32" else None,
        )
        try:
            output, truncated = await self._read(process)
            code = await process.wait()
        finally:
            _kill_group(process)  # on timeout/cancellation too: never leave children running
        text = output.decode("utf-8", errors="replace")
        if truncated:
            text += f"\n[output exceeded {self._max_output} bytes; the process was stopped]"
        if code != 0 or truncated:
            return ToolOutput.text(f"exit code {code}\n{text}", is_error=True)
        return ToolOutput.text(text)

    async def reconcile(self, context: ToolContext) -> ToolOutput | None:
        return None

    async def _read(self, process: asyncio.subprocess.Process) -> tuple[bytes, bool]:
        assert process.stdout is not None  # noqa: S101  # guaranteed by stdout=PIPE
        buffer = bytearray()
        while chunk := await process.stdout.read(65536):
            buffer.extend(chunk)
            if len(buffer) > self._max_output:
                _kill_group(process)
                return bytes(buffer[: self._max_output]), True
        return bytes(buffer), False

    def _limits(self) -> None:  # runs in the child before exec (POSIX)
        import resource

        def cap(which: int, value: int | None) -> None:
            if value is None:
                return
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(which, (value, value))

        cap(resource.RLIMIT_CPU, self._cpu)
        cap(resource.RLIMIT_FSIZE, self._fsize)
        cap(resource.RLIMIT_NOFILE, self._nofile)
        cap(resource.RLIMIT_CORE, 0)


def _kill_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        if sys.platform == "win32":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)


def command_tool(
    name: str,
    argv: Sequence[str],
    *,
    description: str,
    parameters: Mapping[str, tuple[str, str | None, str | None]],
    effect: ToolEffect,
    idempotent: bool | None = None,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    timeout_s: float = 30.0,
) -> CommandTool:
    """A CommandTool from plain data (for configured commands): `parameters` maps each name to
    (description, default or None when required, regular expression the whole value must
    match). Patterns are anchored here: a pattern alone would match anywhere in the value."""
    from pydantic import ConfigDict, Field, create_model

    fields: dict[str, Any] = {}
    for key, (about, default, pattern) in parameters.items():
        constraint = Field(
            default=... if default is None else default,
            description=about or None,
            pattern=None if pattern is None else f"^(?:{pattern})$",
            max_length=1000,
        )
        fields[key] = (str, constraint)
    model = create_model(f"{name}_parameters", __config__=ConfigDict(extra="forbid"), **fields)
    return CommandTool(
        name,
        argv,
        description=description,
        parameters=model,
        effect=effect,
        idempotent=idempotent,
        cwd=cwd,
        env=env,
        timeout_s=timeout_s,
    )
