"""CommandTool: no shell, no option injection, scrubbed env, enforced limits. Not a sandbox."""

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from hollis.runtime.tools import Isolation, ToolArgumentError, ToolContext, ToolEffect
from hollis.tools.builtin import CommandTool

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and rlimits")
CONTEXT = ToolContext(run_id=uuid4(), invocation_id="i", attempt=1)
PY = sys.executable


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str


def _script(code: str, **kwargs: object) -> CommandTool:
    return CommandTool(
        "run_script",
        [PY, "-c", code, "{text}"],
        description="test script",
        parameters=Args,
        effect=ToolEffect.READ_ONLY,
        **kwargs,  # type: ignore[arg-type]
    )


async def _run(tool: CommandTool, text: str) -> tuple[str, bool]:
    output = await tool.invoke(tool.validate({"text": text}), CONTEXT)
    return "".join(getattr(p, "text", "") for p in output.parts), output.is_error


def test_declares_process_isolation_never_sandbox() -> None:
    assert _script("pass").descriptor.isolation is Isolation.PROCESS


async def test_shell_metacharacters_stay_literal(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    tool = _script("import sys; print(sys.argv[1])")
    out, error = await _run(tool, f"hi; touch {marker} && echo $(whoami) | cat")
    assert not error
    assert out.strip() == f"hi; touch {marker} && echo $(whoami) | cat"
    assert not marker.exists()


async def test_option_injection_is_refused() -> None:
    tool = _script("pass")
    with pytest.raises(ToolArgumentError, match="may not start with '-'"):
        tool.validate({"text": "--upload-pack=evil"})
    allowed = _script("import sys; print(sys.argv[1])", allow_leading_dash=frozenset({"text"}))
    assert (await _run(allowed, "-n"))[0].strip() == "-n"


async def test_environment_is_scrubbed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOLLIS_TEST_SECRET", "s3cr3t-token-value")
    tool = _script("import os, sys; print(sorted(os.environ))")
    out, _ = await _run(tool, "x")
    assert "HOLLIS_TEST_SECRET" not in out


async def test_nonzero_exit_is_an_error_result() -> None:
    out, error = await _run(_script("import sys; print('bad'); sys.exit(3)"), "x")
    assert error
    assert out.startswith("exit code 3")


async def test_output_cap_stops_the_process() -> None:
    out, error = await _run(_script("print('y' * 1_000_000)", max_output_bytes=1000), "x")
    assert error
    assert "output exceeded 1000 bytes" in out
    assert len(out) < 1200


async def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time;"
        f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']);"
        f"open({str(pidfile)!r}, 'w').write(str(c.pid)); time.sleep(30)"
    )
    tool = _script(code)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(1.0):
            await _run(tool, "x")
    child = int(pidfile.read_text())
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)  # the grandchild was killed with the group


async def test_file_size_limit_is_enforced(tmp_path: Path) -> None:
    target = tmp_path / "big.bin"
    code = f"open({str(target)!r}, 'wb').write(b'x' * 10_000_000)"
    _, error = await _run(_script(code, max_file_bytes=1_000_000), "x")
    assert error
    assert target.stat().st_size <= 1_000_000


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["python3", "{text}"], "absolute path"),
        (["{text}"], "fixed program"),
        ([PY, "pre{text}"], "whole"),
        ([PY, "{missing}"], "whole"),
    ],
)
def test_unsafe_templates_are_rejected_at_definition(argv: list[str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CommandTool("t", argv, description="d", parameters=Args, effect=ToolEffect.READ_ONLY)


def test_parameters_must_forbid_extra_fields() -> None:
    class Loose(BaseModel):
        text: str

    with pytest.raises(ValueError, match="extra='forbid'"):
        CommandTool(
            "t", [PY, "{text}"], description="d", parameters=Loose, effect=ToolEffect.READ_ONLY
        )
