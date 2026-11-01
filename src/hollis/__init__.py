"""Hollis: a model-agnostic platform for building, running, and orchestrating agents.

from hollis import Hollis

with Hollis.open() as hollis:
    run = hollis.runs.start("list the files", agent="helper")
    print(run.wait().phase)
"""

from collections.abc import Sequence
from importlib.metadata import version
from typing import TYPE_CHECKING, Any

__version__ = version("hollis")

if TYPE_CHECKING:
    from hollis.sdk import AsyncHollis, Hollis

__all__ = ["AsyncHollis", "Hollis", "__version__", "main"]


def __getattr__(name: str) -> Any:
    # The SDK loads the whole platform; import it on first use so `import hollis` stays light.
    if name in ("Hollis", "AsyncHollis"):
        from hollis import sdk

        return getattr(sdk, name)
    raise AttributeError(f"module 'hollis' has no attribute {name!r}")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the `hollis` command. Returns the process exit code."""
    from hollis.cli import run

    return run(argv)
