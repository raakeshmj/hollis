"""The Anthropic SDK is an optional extra: nothing outside its adapter may import it."""

import subprocess
import sys

CORE_MODULES = [
    "hollis",
    "hollis.runtime.runner",
    "hollis.runtime.redaction",
    "hollis.adapters.memory_store",
    "hollis.adapters.sqlite_store",
    "hollis.adapters.fake_provider",
    "hollis.adapters.credentials",
]


def test_core_modules_do_not_load_the_anthropic_sdk() -> None:
    code = (
        "import importlib, sys\n"
        f"for name in {CORE_MODULES!r}: importlib.import_module(name)\n"
        "loaded = sorted(m for m in ('anthropic', 'httpx2', 'opentelemetry') if m in sys.modules)\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run(  # noqa: S603  # fixed interpreter and code
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""
