"""Tools that let a model read large tool results on demand (ADR-0019).

When a result is too large to keep in context, the runner stores it whole as an artifact and the
model sees a preview with the artifact id. These tools read a slice or search the full text.

Access boundary: an artifact is looked up by the *current run's* id (from `ToolContext`), so a
model can only read artifacts its own run produced, whatever id it supplies. Content produced by
an external tool is framed as untrusted data, as the original result was.

Both tools are read-only and trusted, so the default policy allows them. Enable them per run like
any other tool (`RunSpec.tools`).
"""

from typing import Annotated

from pydantic import Field

from hollis.runtime.artifacts import ARTIFACT_ID_PATTERN, Artifact, ArtifactStore
from hollis.runtime.results import frame_untrusted
from hollis.runtime.tools import Tool, ToolContext, ToolEffect, ToolError, ToolTrust
from hollis.tools.function import FunctionTool

ArtifactId = Annotated[
    str, Field(pattern=ARTIFACT_ID_PATTERN, description="Artifact id from a truncated result")
]


def artifact_tools(
    store: ArtifactStore, *, prefix: str = "artifact", max_chars: int = 4000
) -> list[Tool]:
    """`<prefix>_read` (a slice by character offset) and `<prefix>_search` (matching lines)."""

    async def load(artifact_id: str, context: ToolContext) -> Artifact:
        artifact = await store.get(context.run_id, artifact_id)
        if artifact is None:
            raise ToolError(f"no artifact {artifact_id} in this run")
        return artifact

    def framed(artifact: Artifact, text: str) -> str:
        if artifact.trust is ToolTrust.EXTERNAL:
            return frame_untrusted(f"artifact {artifact.ref.artifact_id}", text)
        return text

    async def read(
        artifact_id: ArtifactId,
        context: ToolContext,
        offset: Annotated[int, Field(ge=0, description="Character offset to start at")] = 0,
        length: Annotated[
            int, Field(ge=1, le=max_chars, description="Characters to return")
        ] = max_chars,
    ) -> str:
        """Read part of a stored tool result (an artifact) by character offset."""
        artifact = await load(artifact_id, context)
        total = artifact.ref.chars
        if offset >= total and total > 0:
            raise ToolError(f"offset {offset} is past the end ({total} characters)")
        end = min(offset + length, total)
        header = f"[artifact {artifact_id}: characters {offset}-{end} of {total}]\n"
        return framed(artifact, header + artifact.text[offset:end])

    async def search(
        artifact_id: ArtifactId,
        query: Annotated[str, Field(min_length=1, max_length=200, description="Text to find")],
        context: ToolContext,
        max_matches: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> str:
        """Find lines containing a text (case-insensitive) in a stored tool result."""
        artifact = await load(artifact_id, context)
        needle = query.casefold()
        matches: list[str] = []
        offset = 0
        count = 0
        for number, line in enumerate(artifact.text.splitlines(keepends=True), start=1):
            if needle in line.casefold():
                count += 1
                if len(matches) < max_matches:
                    shown = line.rstrip("\n")
                    if len(shown) > 300:
                        shown = shown[:300] + "…"
                    matches.append(f"line {number} (offset {offset}): {shown}")
            offset += len(line)
        if not matches:
            return framed(artifact, f"[no lines in {artifact_id} contain {query!r}]")
        more = f"\n[{count - len(matches)} more matches not shown]" if count > len(matches) else ""
        header = f"[{count} matching line(s) in artifact {artifact_id}]\n"
        return framed(artifact, header + "\n".join(matches) + more)

    return [
        FunctionTool(read, name=f"{prefix}_read", effect=ToolEffect.READ_ONLY),
        FunctionTool(search, name=f"{prefix}_search", effect=ToolEffect.READ_ONLY),
    ]
