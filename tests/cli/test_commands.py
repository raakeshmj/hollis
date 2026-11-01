"""The CLI through its entry point: output, JSON, exit codes, errors. Embedded workspaces, no
network; approvals are left waiting (--approve stop) and decided with `hollis approvals`."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

import hollis

ANSI = re.compile(r"\x1b\[")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    for name in ("HOLLIS_WORKSPACE", "HOLLIS_URL", "HOLLIS_TOKEN", "NO_COLOR", "FORCE_COLOR"):
        monkeypatch.delenv(name, raising=False)
    assert hollis.main(["init"]) == 0
    return tmp_path


def cli(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    code = hollis.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def cli_json(capsys: pytest.CaptureFixture[str], *args: str) -> Any:
    code, out, err = cli(capsys, "--json", *args)
    assert code == 0, err
    return json.loads(out)


def test_init_creates_a_workspace_with_a_starter(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert (workspace / "hollis.toml").is_file()
    assert [a["name"] for a in cli_json(capsys, "agents", "list")] == ["assistant"]
    assert [c["name"] for c in cli_json(capsys, "knowledge", "collections")] == ["docs"]
    code, _, err = cli(capsys, "init")
    assert code == 0
    assert "already exists" in err


def test_run_completes_and_prints_the_answer(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    code, out, _ = cli(capsys, "run", "list", "the", "files")
    assert code == 0
    assert "completed" in out
    assert "[demo]" in out
    assert not ANSI.search(out), "no colors when output is not a terminal"


def test_approvals_wait_and_are_decided_later(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    code, out, _ = cli(capsys, "run", "--approve", "stop", "write notes.txt: from the cli")
    assert code == 12  # waiting for approval
    assert "needs approval" not in out
    assert not (workspace / "notes.txt").exists()

    [pending] = cli_json(capsys, "approvals", "list")
    assert pending["tool"] == "fs_write_file"
    code, out, _ = cli(capsys, "approvals", "approve", pending["run_id"][:8], "--reason", "ok")
    assert code == 0
    assert "completed" in out
    assert (workspace / "notes.txt").read_text() == "from the cli"
    detail = cli_json(capsys, "runs", "show", pending["run_id"][:8])
    assert detail["turns"][0]["tool_calls"][0]["approval_reason"] == "ok"


def test_denying_from_the_command_line(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert cli(capsys, "run", "--approve", "deny", "write x.txt: no")[0] == 0
    assert not (workspace / "x.txt").exists()


def test_json_output_is_machine_readable(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    detail = cli_json(capsys, "run", "list files")
    assert detail["summary"]["phase"] == "completed"
    runs = cli_json(capsys, "runs", "list")
    assert runs["total"] == 1
    status = cli_json(capsys, "status")
    assert status["health"]["status"] == "ok"
    events = cli_json(capsys, "runs", "events", detail["summary"]["run_id"])
    assert events[-1]["type"] == "run_completed"
    code, out, _ = cli(capsys, "runs", "follow", "--json", detail["summary"]["run_id"])
    assert code == 0
    lines = [json.loads(line) for line in out.splitlines()]
    assert lines[-1]["end"]["phase"] == "completed"


def test_errors_have_exit_codes_and_json(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    code, _, err = cli(capsys, "runs", "show", "00000000-0000-4000-8000-000000000000")
    assert code == 4
    assert err.startswith("error:")
    code, _, err = cli(capsys, "--json", "agents", "show", "nobody")
    assert code == 4
    assert json.loads(err)["error"]["code"] == "not_found"
    code, _, err = cli(capsys, "agents", "create", "x")
    assert code == 2  # usage: --model is required
    code, _, _ = cli(capsys, "run", "--detach", "hello")
    assert code == 2, "detaching needs a server"


def test_no_workspace_is_explained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HOLLIS_WORKSPACE", raising=False)
    monkeypatch.delenv("HOLLIS_URL", raising=False)
    code, _, err = cli(capsys, "status")
    assert code == 4
    assert "hollis init" in err


def test_managing_agents_models_knowledge_memory_and_tokens(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsys.readouterr()
    assert (
        cli(
            capsys,
            "agents",
            "create",
            "reviewer",
            "-m",
            "demo",
            "--tool",
            "fs_read_file",
            "--memory",
            "team:review",
        )[0]
        == 0
    )
    assert cli(capsys, "agents", "update", "reviewer", "--description", "reviews")[0] == 0
    agent = cli_json(capsys, "agents", "show", "reviewer")
    assert (agent["version"], agent["description"], agent["memory"]["write"]) == (
        2,
        "reviews",
        ["team:review"],
    )
    assert cli(capsys, "agents", "delete", "reviewer", "--yes")[0] == 0

    monkeypatch.delenv("HOLLIS_CLI_TEST_KEY", raising=False)
    add = ["models", "add", "remote", "--protocol", "openai-chat-completions", "--model", "m"]
    add += ["--base-url", "https://llm.example/v1", "--locality", "external"]
    code, out, _ = cli(capsys, *add, "--auth-env", "HOLLIS_CLI_TEST_KEY")
    assert code == 0
    assert "missing credential" in out
    assert "HOLLIS_CLI_TEST_KEY" in (workspace / "hollis.toml").read_text()
    assert cli(capsys, "models", "remove", "remote")[0] == 0

    (workspace / "guide.md").write_text("# Guide\n\nRotate keys every 90 days.\n")
    assert cli(capsys, "knowledge", "add", "docs", "guide.md", "--label", "team:sec")[0] == 0
    hits = cli_json(capsys, "knowledge", "search", "rotate", "keys")["hits"]
    assert hits[0]["source_key"] == "guide.md"

    written = cli_json(
        capsys, "memory", "remember", "Releases ship on Tuesdays", "--scope", "team:ops"
    )
    memory_id = written["memory"]["memory_id"]
    assert cli(capsys, "memory", "forget", memory_id, "--reason", "test", "--yes")[0] == 0

    token = cli_json(capsys, "tokens", "create", "--label", "ci")
    assert token["token"].startswith("hx_")
    assert cli(capsys, "tokens", "revoke", token["token_id"])[0] == 0
    assert cli(capsys, "principals", "create", "olive", "--role", "viewer")[0] == 0
    assert {p["name"] for p in cli_json(capsys, "principals", "list")} == {"admin", "olive"}
    assert cli(capsys, "config", "check")[0] == 0
    assert cli(capsys, "doctor")[0] == 0


def test_completion_scripts_and_the_ascii_mark(
    workspace: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    capsys.readouterr()
    code, out, _ = cli(capsys, "completion", "zsh")
    assert code == 0
    assert "_HOLLIS_COMPLETE" in out
    monkeypatch.setenv("HOLLIS_ASCII", "1")
    code, out, _ = cli(capsys, "status")
    assert "`-#" in out
    monkeypatch.delenv("HOLLIS_ASCII")
    monkeypatch.setenv("FORCE_COLOR", "1")
    _, colored, _ = cli(capsys, "runs", "list")
    assert ANSI.search(colored) or "No runs yet" in colored


async def test_a_new_server_revokes_the_token_a_crashed_one_left(tmp_path: Path) -> None:
    from hollis.cli.context import retire_stale_marker, write_server_file
    from hollis.platform.config import initialize
    from hollis.platform.errors import Unauthenticated
    from hollis.platform.schemas import TokenInput
    from hollis.platform.services import Platform

    initialize(tmp_path, "crash")
    platform = await Platform.open(tmp_path)
    try:
        admin = await platform.local_admin()
        left = await platform.create_token(admin, TokenInput(principal="admin", label="local CLI"))
        write_server_file(
            tmp_path, url="http://127.0.0.1:1", token=left.token, token_id=left.token_id
        )
        assert oct((tmp_path / ".hollis" / "server.json").stat().st_mode & 0o777) == "0o600"

        assert await retire_stale_marker(platform, tmp_path)
        with pytest.raises(Unauthenticated):
            await platform.authenticate(left.token)
        assert not await retire_stale_marker(platform, tmp_path), "already revoked"
    finally:
        await platform.close()


def test_delegation_from_the_cli(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(capsys, "agents", "create", "researcher", "-m", "demo")[0] == 0
    code, _, err = cli(
        capsys,
        "agents",
        "create",
        "lead",
        "-m",
        "demo",
        "--delegate",
        "researcher",
        "--max-depth",
        "1",
    )
    assert code == 0, err
    shown = cli_json(capsys, "agents", "show", "lead")
    assert shown["delegates"] == ["researcher"]
    assert shown["delegation"]["max_depth"] == 1

    code, out, err = cli(capsys, "run", "-a", "lead", "@researcher what is hollis?")
    assert code == 0, err
    assert "From the team" in out
    [root] = cli_json(capsys, "runs", "list", "--roots")["items"]
    assert root["children"] == 1

    code, out, _ = cli(capsys, "runs", "tree", root["run_id"][:8])
    assert code == 0
    assert "orchestration" in out
    assert "researcher" in out
    tree = cli_json(capsys, "runs", "tree", root["run_id"])
    assert len(tree["nodes"]) == 2

    plan = workspace / "plan.toml"
    plan.write_text('[[steps]]\nid = "ask"\nagent = "researcher"\nobjective = "what is hollis?"\n')
    code, out, err = cli(capsys, "run", "--plan", str(plan), "Plan it")
    assert code == 0, err
    assert "Plan finished: 1 of 1 steps completed." in out
    bad = workspace / "bad.toml"
    bad.write_text('[[steps]]\nid = "a"\nagent = "nobody"\nobjective = "x"\n')
    assert cli(capsys, "run", "--plan", str(bad), "x")[0] != 0
