from importlib.metadata import version

import pytest

import hollis


def test_version_matches_installed_metadata() -> None:
    assert hollis.__version__ == version("hollis")


def test_version_flag_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert hollis.main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"hollis {hollis.__version__}"


def test_no_arguments_prints_the_mark_and_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert hollis.main([]) == 0
    out = capsys.readouterr().out
    assert "Usage: hollis" in out
    assert "hollis init" in out


def test_unknown_option_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert hollis.main(["--no-such-flag"]) == 2
    assert "No such option" in capsys.readouterr().err


def test_importing_hollis_does_not_load_the_platform() -> None:
    import subprocess
    import sys

    code = "import sys, hollis; print('hollis.platform' in sys.modules, 'click' in sys.modules)"
    result = subprocess.run(  # noqa: S603  # fixed interpreter and code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.split() == ["False", "False"]
