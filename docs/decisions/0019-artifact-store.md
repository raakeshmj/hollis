# ADR-0019: Artifact store for complete tool results

- Status: Accepted (M4)
- Date: 2026-10-31

## Context

M3 truncated tool results above `max_tool_output_chars` and lost the rest (ADR-0014). Large results such as logs, query output and documents are often what an agent needs, but injecting them whole into every later model call wastes budget and cache. Four things with different lifecycles had been conflated:
- run history (the event log, permanent);
- full tool output;
- working context (per call);
- persistent memory (cross-run, ADR-0020).

## Decision

**Port and stores.**
- `ArtifactStore` port in the runtime: `put`, `get`, `delete_run`.
- Adapters: `InMemoryArtifactStore`, and `SqliteArtifactStore` in its own database file (mode 0600, `secure_delete` on).
- Artifacts are keyed by (run id, content digest): a store never returns another run's artifact, and storing identical content twice is a no-op. That covers a crash between storing and recording.

**Runner behaviour (opt-in).** When the runner has an artifact store and a result is longer than `ContextPolicy.artifact_threshold_chars` (4,000 by default):
- the complete result is redacted (registered secrets) and stored;
- the run log records a bounded preview (`artifact_preview_chars`, 2,000) with a marker naming the artifact, plus an `ArtifactRef` (id, size, digest) on `ToolResultRecorded`.

If storing fails, the result is truncated as in M3. It is never lost from the record.

**Retrieval.**
- `artifact_read(artifact_id, offset, length)` and `artifact_search(artifact_id, query)`: read-only, trusted, so the default policy allows them. They're enabled per run like any tool.
- They resolve ids against the current run (`ToolContext.run_id`) only.
- Content from external tools stays framed as untrusted data.

**Context.** Artifact-backed results of older turns are the first lever the context engine uses under pressure (ADR-0018). They're replaced by a reference the model can follow.

**Lifecycle.** Artifacts belong to their run. `delete_run` removes a run's artifacts and leaves its event log, which holds the preview. Run deletion as a whole is still governed by ADR-0010.

## Alternatives considered

- **Store full results in the event log:** inflates every fold and keeps large payloads in the permanent record.
- **Store them as memory:** different lifecycle and scope; memory is curated and cross-run.
- **Content-addressed storage shared across runs:** saves space, but couples runs' access and deletion.
- **A file-system blob directory:** possible later for very large artifacts. SQLite keeps M4 transactional and simple.

## Trade-offs

- The full redacted output is duplicated outside the event log. The log holds only the preview.
- Text only. Binary results remain `ReferencePart` descriptions, as in M3.
- Results above 20 million characters are truncated before storing, with a marker.

## Consequences

- **Evidence:** `tests/tools/test_artifacts.py`: store contract on both adapters, redaction before storage, bounded previews, on-demand read and search, cross-run access refused, external framing kept, clearing under pressure, failure fallback.
- Mutation checks cover run scoping and redaction.
