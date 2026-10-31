"""The runtime core may import only the standard library, pydantic, and itself (ADR-0002).

This is an allowlist: any new third-party or vendor SDK import, or any import of
`hollis.adapters`, fails. `importlib` and `__import__()` are refused to close the obvious
dynamic-import loophole. Imports under `if TYPE_CHECKING:` are checked too.
"""

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

SRC = Path(__file__).parents[2] / "src"
CORE_PACKAGE = "hollis.runtime"
ALLOWED_THIRD_PARTY = frozenset({"pydantic", "pydantic_core"})
FORBIDDEN_STDLIB = frozenset({"importlib"})


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    target: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {CORE_PACKAGE} must not import {self.target!r}"


def is_allowed(module: str) -> bool:
    if module == CORE_PACKAGE or module.startswith(CORE_PACKAGE + "."):
        return True
    top = module.partition(".")[0]
    if top == "hollis" or top in FORBIDDEN_STDLIB:
        return False
    return top in sys.stdlib_module_names or top in ALLOWED_THIRD_PARTY


def _module_name(path: Path, src: Path) -> str:
    parts = path.relative_to(src).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imported_modules(
    node: ast.Import | ast.ImportFrom, module: str, is_package: bool
) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level == 0:
        return [node.module or ""]
    package = module if is_package else module.rpartition(".")[0]
    base_parts = package.split(".")
    if node.level > 1:
        base_parts = base_parts[: -(node.level - 1)]
    base = ".".join(base_parts)
    if node.module:
        return [f"{base}.{node.module}"]
    # `from .. import name` may import submodules; check each name as one.
    return [f"{base}.{alias.name}" for alias in node.names]


def find_violations(package_dir: Path, src: Path) -> list[Violation]:
    violations: list[Violation] = []
    for path in sorted(package_dir.rglob("*.py")):
        module = _module_name(path, src)
        is_package = path.name == "__init__.py"
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, ast.Import | ast.ImportFrom):
                violations += [
                    Violation(path, node.lineno, target)
                    for target in _imported_modules(node, module, is_package)
                    if not is_allowed(target)
                ]
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "__import__"
            ):
                violations.append(Violation(path, node.lineno, "__import__()"))
    return violations


def test_runtime_core_respects_import_boundaries() -> None:
    core = SRC / "hollis" / "runtime"
    assert len(list(core.rglob("*.py"))) >= 6, "checker found too few files; is the path right?"
    violations = find_violations(core, SRC)
    assert not violations, "\n".join(map(str, violations))


# --- the checker itself must detect forbidden imports ------------------------------------


def _check_source(tmp_path: Path, source: str) -> list[Violation]:
    core = tmp_path / "hollis" / "runtime"
    core.mkdir(parents=True)
    (core / "__init__.py").write_text("")
    (core / "module.py").write_text(source)
    return find_violations(core, tmp_path)


@pytest.mark.parametrize(
    "source",
    [
        "import openai",
        "import anthropic.types",
        "from google import genai",
        "from hollis.adapters import memory_store",
        "from hollis.adapters.sqlite_store import SqliteRunStore",
        "from ..adapters.sqlite_store import SqliteRunStore",
        "from .. import adapters",
        "import hollis",
        "import importlib",
        "from importlib import import_module",
        "__import__('openai')",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import httpx",
        "def f():\n    import requests",
    ],
)
def test_checker_detects_forbidden_import(tmp_path: Path, source: str) -> None:
    assert _check_source(tmp_path, source), f"not detected: {source!r}"


def test_checker_accepts_allowed_imports(tmp_path: Path) -> None:
    source = "\n".join(
        [
            "from __future__ import annotations",
            "import json",
            "from collections.abc import Sequence",
            "from pydantic import BaseModel",
            "from pydantic_core import PydanticCustomError",
            "from hollis.runtime.state import fold",
            "from .events import EventEnvelope",
            "from . import model",
        ]
    )
    assert _check_source(tmp_path, source) == []
