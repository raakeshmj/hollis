"""The web application as shipped: the committed build is complete, its API types match the
server, and (opt-in) the browser journeys pass in Google Chrome against a real server."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from hollis.server import openapi

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "hollis" / "server" / "static"
WEB = ROOT / "web"


def test_the_committed_build_references_only_files_it_contains() -> None:
    index = (STATIC / "index.html").read_text()
    referenced = re.findall(r'(?:src|href)="/([^"#?]+)"', index)
    assert any(path.startswith("assets/") and path.endswith(".js") for path in referenced)
    for path in referenced:
        assert (STATIC / path).is_file(), f"index.html references a missing file: {path}"
    css = next(STATIC.glob("assets/*.css")).read_text()
    for font in re.findall(r"url\(/?(assets/[^)]+)\)", css):
        assert (STATIC / font).is_file(), f"the stylesheet references a missing font: {font}"
    assert "<script>" not in index, "no inline scripts: the CSP forbids them"


def test_the_web_app_was_built_from_the_current_api_contract() -> None:
    committed = json.loads((WEB / "openapi.json").read_text())
    assert committed == json.loads(openapi.dumps()), (
        "web/openapi.json is out of date: run `make web-api` and rebuild the web app"
    )


@pytest.mark.ui
@pytest.mark.skipif(
    os.environ.get("HOLLIS_UI_TESTS") != "1", reason="set HOLLIS_UI_TESTS=1 to drive Chrome"
)
def test_browser_journeys_pass_in_chrome() -> None:
    npx = shutil.which("npx")
    if npx is None or not (WEB / "node_modules").is_dir():
        pytest.fail("the UI tests need Node and `make web-install`")
    result = subprocess.run(  # noqa: S603  # fixed command
        [npx, "playwright", "test"],
        cwd=WEB,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
