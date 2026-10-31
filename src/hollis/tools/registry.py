"""The tool registry: an explicit, operator-assembled catalog (ADR-0015).

Nothing is discovered or loaded implicitly. A tool is available only if code registers it, and a
run can call only the registered tools it names in `RunSpec.tools`.
"""

from collections.abc import Iterable, Iterator

from hollis.runtime.tools import Tool


class DuplicateTool(ValueError):
    pass


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools:
            self.add(t)

    def add(self, tool: Tool) -> Tool:
        name = tool.descriptor.name
        if name in self._tools:
            raise DuplicateTool(f"a tool named {name!r} is already registered")
        self._tools[name] = tool
        return tool

    def extend(self, tools: Iterable[Tool]) -> None:
        for t in tools:
            self.add(t)

    def remove(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)
