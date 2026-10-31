"""Hollis: a model-agnostic platform for building, running, and orchestrating agents."""

import argparse
from collections.abc import Sequence
from importlib.metadata import version

__version__ = version("hollis")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the `hollis` CLI. Returns the process exit code."""
    parser = argparse.ArgumentParser(prog="hollis", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0
