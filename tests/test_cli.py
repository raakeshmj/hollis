from importlib.metadata import version

import pytest

import hollis


def test_version_matches_installed_metadata() -> None:
    assert hollis.__version__ == version("hollis")


def test_version_flag_prints_version_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        hollis.main(["--version"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"hollis {hollis.__version__}"


def test_no_arguments_prints_help_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert hollis.main([]) == 0
    assert "usage: hollis" in capsys.readouterr().out


def test_unknown_argument_exits_with_usage_error() -> None:
    with pytest.raises(SystemExit) as exc_info:
        hollis.main(["--no-such-flag"])
    assert exc_info.value.code == 2
