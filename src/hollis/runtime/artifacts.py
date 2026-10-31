"""The artifact port: durable storage for complete tool results (ADR-0019).

M3 truncated large tool results and lost the rest. With an `ArtifactStore`, the runner stores the
complete (redacted) result, records a bounded preview plus an `ArtifactRef` in the run log, and
the model can read the rest on demand through the opt-in artifact tools.

Artifacts are scoped to the run that produced them: every read names the run, and a store never
returns another run's artifact. They are content-addressed within a run, so storing the same
result twice (for example after a crash between storing and recording) yields the same id.

Artifacts are not memory and not run history. They are derived copies of tool results with the
run's lifecycle: deleting a run's artifacts leaves its event log (which holds the preview)
unchanged.
"""

import hashlib
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hollis.runtime.tools import ToolTrust

ARTIFACT_ID_PATTERN = r"^art_[0-9a-f]{24}$"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ArtifactRef(_Frozen):
    """A bounded, traceable reference to a stored artifact, recorded in the run log."""

    artifact_id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    chars: int = Field(ge=0)
    digest: str
    """sha256 of the UTF-8 content."""
    media_type: str = "text/plain"


class Artifact(_Frozen):
    ref: ArtifactRef
    run_id: UUID
    source: str
    """What produced it, e.g. the invocation id of a tool call."""
    trust: ToolTrust
    """Trust of the producing tool. Content from external tools is framed as untrusted data
    whenever it is shown to a model."""
    text: str


class ArtifactStore(Protocol):
    async def put(
        self,
        run_id: UUID,
        text: str,
        *,
        source: str,
        trust: ToolTrust,
        media_type: str = "text/plain",
    ) -> ArtifactRef:
        """Store `text` for `run_id`. Idempotent: identical content returns the same ref."""
        ...

    async def get(self, run_id: UUID, artifact_id: str) -> Artifact | None:
        """The artifact if it belongs to `run_id`, else None (never another run's artifact)."""
        ...

    async def delete_run(self, run_id: UUID) -> int:
        """Delete every artifact of a run; returns how many were deleted."""
        ...


def artifact_ref(text: str, media_type: str = "text/plain") -> ArtifactRef:
    digest = hashlib.sha256(text.encode()).hexdigest()
    return ArtifactRef(
        artifact_id=f"art_{digest[:24]}",
        chars=len(text),
        digest=f"sha256:{digest}",
        media_type=media_type,
    )
