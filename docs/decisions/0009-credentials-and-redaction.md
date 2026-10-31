# ADR-0009: Credential boundary and redaction

- Status: Accepted (M2)
- Date: 2026-10-31

## Context

M1 stored run input, instructions, responses and failure messages verbatim, and had no credentials at all (requirements: "Data persistence and secret handling"). M2 introduces a real API key and real provider error messages. Requirements for M2: credentials referenced rather than stored; redaction before persistence and logging; sanitized provider errors; tests proving it. All of this must happen without breaking recovery, which resends the identical request under the same operation id.

## Decision

**Credential boundary** (`hollis.adapters.credentials`):
- `require_env_secret(name)` returns a `pydantic.SecretStr`, or raises `MissingCredential` naming the variable. It only runs when called, so importing Hollis or running tests never needs a key.
- The secret goes straight to the provider client.
- It is never placed in `RunSpec`, `ModelRequest`, `RunCreated` or any event. Runs record the provider *name* only.
- `.env.example` lists variable names. `.env` is git-ignored.

**Redaction** (`hollis.runtime.redaction.Redactor`):
- **Registered values.** The composition root registers exact secret values (for example the API key; 8 characters minimum). This works reliably wherever they appear.
- **Patterns.** Best effort, for common credential shapes: `sk-ant-…`, `Bearer …`, and `x-api-key`/`api_key`/`authorization` followed by a value.
- **Where the runner applies it:**
  - Failure messages (`ModelCallFailed.failure.message`, `RunFailed.message`) and its own log records: registered values and patterns.
  - Recorded response content: registered values only. Patterns aren't applied, so legitimate text about key formats isn't mangled.
- **Run input and instructions are never rewritten.** Rewriting would make recovery send a different request than the first attempt. `Runner.create` **rejects** input containing a registered secret (`SecretInRunInput`) before anything is persisted.

**Adapter sanitization:**
- Provider messages are built from structured fields: status, error type, and the API's message truncated to 300 characters.
- Raw request or response bodies and headers are never included.
- SDK OpenTelemetry is disabled for adapter-built clients (ADR-0008).

## Alternatives considered

- **Redacting input before persisting:** breaks same-request recovery and the fake's request-aware deduplication. Rejected in favour of refusing such input.
- **Pattern redaction on all content:** false positives would corrupt model output and user data.
- **A secrets manager or vault integration:** out of scope for M2. The credential module is the seam for one later.
- **Encrypting the store:** doesn't address logs or diagnostics, and is deferred (see ADR-0010).

## Trade-offs and limitations (stated plainly)

- **Arbitrary secrets in user-supplied text are not detected.** Only registered values are refused in input. Pattern-shaped secrets in input, and secrets of any unknown shape, are persisted verbatim.
- Patterns are a heuristic. They will miss some secrets and may redact some harmless diagnostic text.
- Model responses can contain sensitive data that isn't a registered secret. It's stored as returned.
- SQLite data is still not encrypted at rest. Files are created with mode `0600`.
- SDK debug logging (`ANTHROPIC_LOG=debug`) is outside Hollis's control and shouldn't be enabled with real keys.

## Consequences

- **Evidence** (`tests/runtime/test_redaction.py`, `tests/adapters/test_anthropic_runtime.py`): a planted key in provider error bodies and response content never appears in serialized events, in the SQLite file bytes, or in captured log records. Input containing a registered key is refused before persistence.
- A future trace exporter must apply the same `Redactor` to span attributes.
