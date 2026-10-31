"""Provider dependencies are optional extras, loaded only by the adapter that needs them."""

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
    "hollis.adapters.config",
    "hollis.gateway",
]
OPTIONAL = ("anthropic", "httpx2", "opentelemetry")


def _loaded_after(code: str) -> set[str]:
    script = (
        "import importlib, sys\n"
        f"{code}\n"
        f"print(','.join(m for m in {OPTIONAL!r} if m in sys.modules))\n"
    )
    result = subprocess.run(  # noqa: S603  # fixed interpreter and code
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    return {m for m in result.stdout.strip().split(",") if m}


def test_core_and_gateway_modules_load_no_provider_dependencies() -> None:
    assert _loaded_after(f"for name in {CORE_MODULES!r}: importlib.import_module(name)") == set()


def test_resolving_an_openai_compatible_deployment_never_loads_the_anthropic_sdk() -> None:
    code = """
from hollis.gateway import Catalog, ModelGateway
catalog = Catalog.from_mapping({"deployments": [{
    "id": "local", "protocol": "openai-chat-completions", "provider": "ollama",
    "model": "qwen3", "locality": "local",
    "endpoint": {"base_url": "http://127.0.0.1:11434/v1"}}]})
ModelGateway(catalog).resolve("local")
"""
    assert _loaded_after(code) == {"httpx2"}
