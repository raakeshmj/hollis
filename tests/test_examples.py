"""The examples run as documented (embedded, demo model: no network, no key)."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(script: str, *args: str) -> str:
    result = subprocess.run(  # noqa: S603  # fixed interpreter and repository scripts
        [sys.executable, str(ROOT / "examples" / script), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return result.stdout


def test_quickstart(tmp_path: Path) -> None:
    out = run("quickstart.py", str(tmp_path))
    assert "answer: [demo]" in out
    assert "approval needed: fs_write_file" in out
    assert "write: completed -> tag the release first" in out
    assert (tmp_path / "hollis.toml").is_file()


def test_approval_rules_decide_by_path(tmp_path: Path) -> None:
    run("quickstart.py", str(tmp_path))  # a workspace with a starter agent
    from hollis import Hollis

    with Hollis.open(tmp_path) as hollis:
        hollis.runs.start("write notes/a.txt: yes").wait()
        hollis.runs.start("write elsewhere.txt: no").wait()
    out = run("approval_rules.py", str(tmp_path))
    assert "approved fs_write_file" in out
    assert "denied fs_write_file" in out
    assert (tmp_path / "notes" / "a.txt").read_text() == "yes"
    assert not (tmp_path / "elsewhere.txt").exists()


def test_orchestration(tmp_path: Path) -> None:
    out = run("orchestration.py", str(tmp_path))
    assert "lead: completed, 4 runs, depth 1" in out
    assert "researcher: completed" in out
    assert "make ship" in out
    assert "41000 euros" in out
    assert "plan: Plan finished: 3 of 3 steps completed." in out
