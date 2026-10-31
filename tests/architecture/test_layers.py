"""Layering beyond the runtime core: tools depend on the runtime port, not on the gateway."""

import ast
from pathlib import Path

SRC = Path(__file__).parents[2] / "src"


def _imports(package: Path) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in package.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add((path.name, node.module))
            elif isinstance(node, ast.Import):
                found.update((path.name, alias.name) for alias in node.names)
    return found


def test_tools_do_not_depend_on_the_gateway_or_runner_internals() -> None:
    offending = {
        (file, module)
        for file, module in _imports(SRC / "hollis" / "tools")
        if module.startswith(("hollis.gateway", "hollis.runtime.runner", "hollis.runtime.state"))
    }
    assert offending == set()


def test_runtime_does_not_depend_on_tool_implementations() -> None:
    offending = {
        (file, module)
        for file, module in _imports(SRC / "hollis" / "runtime")
        if module.startswith(("hollis.tools", "hollis.adapters", "hollis.gateway", "hollis.memory"))
    }
    assert offending == set()


def test_memory_depends_on_ports_not_on_adapters_gateway_or_the_runner() -> None:
    """Memory uses the runtime's ports (model, memory, tools) and must stay storage- and
    provider-independent: SQLite lives in adapters, models are reached through the gateway."""
    offending = {
        (file, module)
        for file, module in _imports(SRC / "hollis" / "memory")
        if module.startswith(("hollis.adapters", "hollis.gateway", "hollis.runtime.runner"))
        or module.split(".")[0] in {"anthropic", "httpx2", "mcp", "sqlite3"}
    }
    assert offending == set()
