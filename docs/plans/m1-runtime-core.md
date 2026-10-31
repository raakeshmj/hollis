# M1 Plan: Runtime Core (revised)

Status: implemented 2026-10-31 on branch `m1-runtime-core`. Supersedes the M0 draft of this plan. See "Implementation notes" at the end for deviations.

## Context
The M1 proposal in `docs/plans/m1-runtime-core.md` (written in M0) was reviewed. The runtime-first direction is approved, but the review found four problems:
- It promised "no completed model step is re-requested", which can't be guaranteed: the outcome is ambiguous if a crash happens after the provider generates a response but before it's recorded.
- It invented a multi-turn loop with nothing to continue it.
- It left event-sourcing and store semantics underspecified.
- It proposed WAL mode without a reason.

This revision fixes those. It keeps M1 a minimal, deterministic, single-model-call runtime that M3 can extend into tool-driven multi-turn execution. Once approved, the first commit replaces `docs/plans/m1-runtime-core.md` with this content, so the repo stays the source of truth.

Branch: `m1-runtime-core`, cut from `m0-foundation` because M0 isn't merged into `master` yet. If M0 gets merged first, I'll rebase onto `master`.

## 1. Architecture decisions (summary)
| # | Decision | Change from M0 docs |
|---|---|---|
| D1 | **Exactly one model call per run** (logical call index 0). Run lifecycle: create → call intent → invoke → record response → complete. | Removes the multi-turn loop and the step-limit budget. A step limit means nothing with one call; budgets move to M2. |
| D2 | **Model calls are at-least-once on invocation, and a response is recorded at most once.** A stable `operation_id` is shared by every attempt of a logical call. A durably recorded response is never followed by another invocation. **No exactly-once claim.** | Replaces "no completed step re-requested". New ADR-0006. |
| D3 | **Intent before every physical attempt.** Each attempt appends `ModelCallStarted(operation_id, attempt=n)` *before* invoking. Attempts and logical generations are observed separately. | Refines ADR-0005. |
| D4 | **Run state is a pure fold over an append-only, versioned event log.** Invalid sequences raise. | ADR-0005 core idea, now specified precisely. |
| D5 | **Optimistic concurrency.** `append(run_id, expected_version, events)` is atomic, and a stale `expected_version` always raises. No leases in M1. | New. |
| D6 | **SQLite uses stdlib `sqlite3`, one connection per operation in a worker thread, default journal mode, `BEGIN IMMEDIATE` transactions, `PRAGMA user_version` migrations.** | Revises ADR-0004: WAL is dropped. |
| D7 | **The same code path handles first execution and recovery.** `Runner.execute(run_id)` folds the state and drives it to a terminal state. There's no separate recovery logic. | New. |
| D8 | **Core imports are an allowlist** (stdlib + `pydantic` + `hollis.runtime`), checked by an AST-based test with a self-test. | Stronger than the planned denylist. |

## 2. Modules (created only when first used)
```
src/hollis/runtime/            # core: no adapter or vendor imports
  model.py     Usage, ModelResponse (c1); ModelRequest, ModelProvider Protocol, ModelProviderError (c5)
  events.py    payload models (c1); EventEnvelope, (de)serialization, schema registry (c2)
  state.py     RunStatus, transition table (c1); RunState, apply(), fold(), InvalidEventSequence (c2)
  store.py     RunStore Protocol, store errors, load_run(store, run_id) (c3)
  runner.py    RunSpec, Runner.create / execute (c6)
src/hollis/adapters/           # may import hollis.runtime; never imported by it
  memory_store.py  InMemoryRunStore (c3)
  sqlite_store.py  SqliteRunStore (c4)
  fake_provider.py ScriptedModelProvider (c5); in src, because it's a first-class testing adapter for users (NFR-3)
tests/runtime/ tests/adapters/ tests/recovery/ tests/architecture/
```
`load_run` lives in the runtime, not in each store. Folding has a single implementation, and stores stay dumb and generic.

## 3. Events
**Envelope** (frozen Pydantic model, `extra="forbid"`):
| Field | Type | Rule |
|---|---|---|
| `event_id` | `UUID` | Unique across the whole store. |
| `run_id` | `UUID` | Same for every event in a log. |
| `sequence` | `int ≥ 1` | Contiguous per run, starting at 1. Equals the run version after the event. |
| `schema_version` | `int ≥ 1` | Version of this event type's payload schema. |
| `occurred_at` | `datetime` | Timezone-aware UTC. Comes from an injected clock. |
| `payload` | discriminated union on `payload.type` | `event_type` is a read-only property returning `payload.type`, and is also stored as a SQLite column. |

**Vocabulary** (all at schema_version 1). Nothing speculative: no Cancelled, no Interrupted, no OutcomeUnknown.
| Event (`type`) | Payload |
|---|---|
| `RunCreated` (`run_created`) | `model: str`, `input: str`, `instructions: str \| None` |
| `ModelCallStarted` (`model_call_started`) | `operation_id: str`, `attempt: int ≥ 1` |
| `ModelResponseRecorded` (`model_response_recorded`) | `operation_id: str`, `attempt: int`, `response: ModelResponse(content: str, usage: Usage \| None)` |
| `RunCompleted` (`run_completed`) | *(empty)* |
| `RunFailed` (`run_failed`) | `operation_id: str`, `error_type: str`, `message: str` |

- `operation_id` is deterministic: `f"{run_id}/model/0"` (call index 0). It's stable across attempts, crashes and processes without any lookup, and M3 increments the index.
- The model request isn't duplicated into `ModelCallStarted`. In M1 it's a pure function of `RunCreated`. Recording the assembled request is deferred to M4 (context assembly).
- **Serialization:** `model_dump_json()` / `model_validate_json()`, with datetimes in ISO-8601. Round-trips are property-tested.
- **Schema evolution:**
  - Stored events are never rewritten.
  - Any change to a payload's shape bumps that type's `schema_version` and adds an *upcaster* (old payload dict → current model) to a registry in `events.py`.
  - Readers must handle every version ever written.
  - An unknown `event_type` or version raises `UnknownEventSchema`.
  - M1 ships version 1 only, with the registry mechanism and a test that an unknown version is rejected. No upcasters yet, and no snapshots.

## 4. State machine
`RunStatus`: `pending → model_call_in_flight → model_responded → completed`. `failed` can only be reached from `model_call_in_flight`.

| From \ Event | run_created | model_call_started | model_response_recorded | run_completed | run_failed |
|---|---|---|---|---|---|
| *(none)* | **pending** | ✗ | ✗ | ✗ | ✗ |
| pending | ✗ | **in_flight** (attempt = 1) | ✗ | ✗ | ✗ |
| in_flight | ✗ | **in_flight** (attempt = prev+1, same op id) | **responded** (op id matches) | ✗ | **failed** (op id matches) |
| responded | ✗ | ✗ | ✗ | **completed** | ✗ |
| completed / failed | ✗ | ✗ | ✗ | ✗ | ✗ |

- `RunState` is frozen and holds: `run_id`, `version`, `status`, `model`, `input`, `instructions`, `operation_id`, `attempts`, `response`, `failure`.
- `apply(state | None, event) -> RunState` and `fold(events) -> RunState` are pure.
- `fold` raises `InvalidEventSequence` on any of: an empty log, a first event that isn't `RunCreated`, a sequence gap or reorder, a mixed `run_id`, a duplicate `event_id`, an illegal transition, an `operation_id` mismatch, or an attempt that isn't prev+1.
- `RunFailed` only follows a call, because M1 has no other failure source. More sources get added when they actually exist.

## 5. Contracts
**RunStore** (`typing.Protocol`, async). The same contract applies to both adapters.
- `append(run_id, expected_version, events: Sequence[EventEnvelope]) -> None`
  - Atomic: all events commit or none do.
  - Creating a run means `expected_version=0` with `[RunCreated]`. Re-creating an existing run raises `ConcurrencyConflict`.
  - Validation raises `InvalidAppend` (a `ValueError`) for: an empty batch, an event whose `run_id` doesn't match, or sequences other than `expected_version+1 … expected_version+n`.
  - Concurrency: `ConcurrencyConflict(run_id, expected, actual)` if the current version ≠ `expected_version`. This is never a silent success.
  - Duplicates: `DuplicateEventId` if any `event_id` already exists anywhere in the store, or is repeated within the batch.
  - Check order: validation, then version, then IDs.
  - A retried append of an already-committed batch therefore surfaces as `ConcurrencyConflict`. The caller re-folds; the store doesn't silently dedupe.
- `read(run_id) -> list[EventEnvelope]` returns events in sequence order, or raises `RunNotFound`.
- The store doesn't validate event semantics. That's the fold's job: the runner applies each event locally before appending it.
- **M1 assumptions:**
  - Single process and a single *intended* active runner per run, with no lease.
  - Safety doesn't depend on that assumption. If two executors advance the same run, both fold version N and both try to append at N. Exactly one succeeds, and the other gets `ConcurrencyConflict` and stops.
  - Every provider invocation is preceded by a *successful* intent append, so two executors can never both invoke for the same attempt number.
  - An executor that's slow but alive can still overlap with a later resume (attempt k in flight while attempt k+1 starts). Both use the same `operation_id`, so a deduplicating provider returns one generation, and OCC lets only one `ModelResponseRecorded` commit.
  - Leases or fencing come in a later cloud milestone.
- `InMemoryRunStore` makes no `await` inside its critical section, so it's atomic under a single event loop (documented).

**ModelProvider** (`Protocol`): `async generate(request: ModelRequest) -> ModelResponse`
- `ModelRequest` holds `operation_id` (the idempotency key), `model`, `instructions` and `input`.
- Raising `ModelProviderError` means a *definitive* rejection, and the runner records `RunFailed`.
- Any other exception means the outcome is ambiguous. The run stays `model_call_in_flight` (resumable) and the exception propagates.

**ScriptedModelProvider:**
- Takes a sequence of `ModelResponse | ModelProviderError`, consumed one per *new* `operation_id`.
- A repeated `operation_id` returns the cached outcome. That models a provider that deduplicates.
- Counters:
  - `invocations`: physical calls.
  - `generations`: logical, i.e. cache misses.
  - `invocations_by_operation`.
- If the script runs out, it raises `ScriptExhausted`, which is loud in tests.

**Runner** `(store, provider, *, clock=utc_now, new_id=uuid4)`:
- `create(spec: RunSpec) -> RunState` appends `RunCreated` at version 0.
- `execute(run_id) -> RunState` loads, then loops on status until terminal:
  - `pending` or `in_flight`: append `ModelCallStarted(attempt=state.attempts+1)`, then invoke. On success, append `ModelResponseRecorded`. On `ModelProviderError`, append `RunFailed`.
  - `responded`: append `RunCompleted`.
  - Terminal: return without touching the store or provider (idempotent).
- Every append goes through `apply()` first, so the runner never persists an event the fold would reject.

## 6. SQLite design
```sql
CREATE TABLE events (
  run_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK (sequence >= 1),
  event_id TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL,
  schema_version INTEGER NOT NULL, occurred_at TEXT NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY (run_id, sequence)
) STRICT;
CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
```
- **Lifecycle:** every operation runs `await asyncio.to_thread(sync_fn)`.
  - `sync_fn` opens `sqlite3.connect(path, timeout=busy_timeout, isolation_level=None)`, does its work and closes the connection in `finally`.
  - A connection never crosses threads, and the default `check_same_thread=True` stays.
  - `:memory:` databases are unsupported because each connection would be a separate database. Tests use files under `tmp_path`.
- **Opening:** `await SqliteRunStore.open(path, busy_timeout=5.0)`.
  - Checks `sqlite3.sqlite_version ≥ 3.37` (needed for STRICT) and fails loudly otherwise.
  - Creates a missing database file with mode `0600`.
  - Runs migrations.
- **Migrations:** an ordered list of statement lists, where index i migrates to `user_version` i+1.
  - Everything runs in one `BEGIN IMMEDIATE` transaction: read `user_version`, apply pending statements one at a time (not `executescript`, which auto-commits), then `PRAGMA user_version = N`, then `COMMIT`.
  - A `user_version` newer than the code raises `SchemaTooNew`. Applying migrations is idempotent.
- **Append transaction:**
  1. `BEGIN IMMEDIATE`, which takes the write lock up front and avoids lock-upgrade deadlocks.
  2. `SELECT COALESCE(MAX(sequence), 0)`, then compare with `expected_version`.
  3. `SELECT` existing `event_id`s from the batch.
  4. `INSERT` all rows.
  5. `COMMIT`, or `ROLLBACK` on any exception.
  - An `IntegrityError` that slips past the pre-checks is mapped to the matching contract error.
- **Errors:**
  - `sqlite3.OperationalError` (including "database is locked" after the busy timeout) becomes `StoreUnavailable(cause)`.
  - A corrupt row (unparseable payload, or an `event_type` column that doesn't match the payload) becomes `CorruptEvent`. Never skipped silently.
- **Journal mode:** default (rollback journal), default `synchronous`. WAL is only added for a measured need.
- **Assumptions:** single process, single active runner, local file system (no network filesystems). Connection-per-operation overhead is accepted for M1.

## 7. Crash-recovery semantics
**The ambiguous outcome:**
- If the process dies after the provider has generated a response but before `ModelResponseRecorded` commits, the log shows `ModelCallStarted` with no response.
- The runtime can't tell whether the provider ran, how many times, or whether it was billed.
- M1 policy: retry with the **same `operation_id`**.
- What's guaranteed:
  1. At most one `ModelResponseRecorded` per logical call (fold plus OCC).
  2. No invocation once a response is durably recorded.
  3. At-least-once invocation.
- Provider-side exactly-once holds **only** if the provider deduplicates on the key. The fake does; real providers may not.
- ADR-0006 records this. M2 then adds a provider idempotency capability and an explicit policy (`retry` vs `fail_on_ambiguous`), and decides then whether that needs a new event.

**Counting:**
- Intents: `ModelCallStarted` events, an upper bound on attempts initiated. An intent with no invocation is possible (a crash in between).
- Physical attempts: `provider.invocations`.
- Logical generations: `provider.generations`.
- Durable responses: `ModelResponseRecorded` events.

| # | Crash point | Injected by | Expected after `execute()` resumes |
|---|---|---|---|
| 1 | after `RunCreated` commits | store wrapper raising `SimulatedCrash` after commit | 1 intent, invocations = 1, generations = 1, completed |
| 2 | after `ModelCallStarted` commits, before invoke | store wrapper | 2 intents (attempt 1, attempt 2), **invocations = 1**, generations = 1 |
| 3 | provider generated, `ModelResponseRecorded` not committed | provider wrapper raising after the inner `generate` returns | invocations = 2, **generations = 1**, recorded content = original generation |
| 4 | after `ModelResponseRecorded` commits | store wrapper | **0 new invocations**, `RunCompleted` appended |
| 5 | after `RunCompleted` commits | store wrapper | no appends, no invocations, state returned |

- `SimulatedCrash` subclasses `BaseException`, so runtime code can't swallow it.
- Every case runs against both stores. The provider instance survives the "crash" because it stands in for external, provider-side state.
- Each final state is compared with an uninterrupted run on `status`, `response`, `model` and `input`.
- **Subprocess test** (SQLite, crash point 4):
  - The child runs `create` and `execute`. Right after `ModelResponseRecorded` commits, a hook prints a marker and blocks, and the parent sends `SIGKILL`.
  - The parent then resumes with a fresh, *empty-script* fake and asserts `completed` with 0 invocations.
  - Documented as process-death recovery only. It doesn't simulate OS or power failure. Skipped on Windows.

## 8. Security and persistence
- **Persisted:** run input text, instructions, model name, response content, token usage, failure type and message. These may contain user data.
- **Never persisted:** credentials or API keys. M1 has no provider configuration in events or `ModelRequest`, and `extra="forbid"` stops undeclared fields from entering payloads.
- **Known gaps (documented):** SQLite data is **not encrypted at rest**, and the DB file is created `0600`. The secret-redaction tests (NFR-4) are written in M2, when real credentials and provider error messages exist.

## 9. Testing
- **Dependencies:**
  - `pytest-asyncio`, with `asyncio_mode = "auto"` and `asyncio_default_fixture_loop_scope = "function"` set explicitly so `filterwarnings = error` stays clean.
  - `hypothesis`, with a `ci` profile (`derandomize=True`, `deadline=None`) selected by an env var in CI. `.hypothesis/` is added to `.gitignore`.
- **Transitions:** parametrized over the full product of status and event type. Every legal cell is asserted to succeed, and every ✗ cell to raise.
- **Fold:** hand-written valid and invalid sequences, plus Hypothesis:
  - A strategy random-walks the state machine to generate valid logs, including retry attempts.
  - Property tests:
    - `fold(valid log)` never raises.
    - `state.version == len(log)`.
    - `fold` equals iterated `apply`.
    - Any mutation of a valid log (drop, swap, duplicate, rewrite `run_id` or `sequence`, change `operation_id` or `attempt`) raises.
- **Serialization:** a round-trip property test for every generated envelope. Unknown `event_type` and unknown `schema_version` are rejected.
- **Store contract:** one suite in `tests/adapters/`, run against a fixture parametrized over the in-memory and SQLite stores. It covers:
  - Create and re-create conflicts.
  - Read order and `RunNotFound`.
  - Stale and future `expected_version`.
  - Duplicate `event_id`, both across runs and within a batch.
  - Bad sequences.
  - Multi-event atomicity: a batch failing on a later event leaves no partial writes.
  - Concurrent appends at the same version via `asyncio.gather`: exactly one wins.
  - A Hypothesis round-trip of generated logs.
- **SQLite-specific:** a fresh database migrates to the latest version, re-opening is a no-op, a too-new `user_version` raises, UPDATE and DELETE are rejected by the triggers, a corrupt row raises `CorruptEvent`, and the file mode is `0600`.
- **Runner:** happy path, `ModelProviderError` → `failed`, an unexpected exception leaves the run in flight and resumable, `execute` on a terminal run is a no-op, and two runners racing produce one response and one `ConcurrencyConflict`.
- Recovery and architecture tests as described in §7 and §10.

## 10. Architecture enforcement (stdlib `ast`, no new dependency)
- `tests/architecture/test_import_boundaries.py` walks `src/hollis/runtime/**/*.py`, including `TYPE_CHECKING` blocks.
- It resolves relative imports to absolute module names.
- **Allowed:** `sys.stdlib_module_names` minus `importlib`, plus `pydantic`, `pydantic_core` and `hollis.runtime.*`. Anything else, including `hollis.adapters` and any vendor SDK, fails the test with the file and line.
- Excluding `importlib`, and flagging `__import__` calls, closes the obvious dynamic-import loophole.
- A self-test runs the checker on synthetic files and asserts it flags `import openai`, `from hollis.adapters import x`, `from ..adapters.sqlite_store import S` and `__import__("x")`, and that it passes a clean file.

## 11. Dependency changes
| Package | Group | Why |
|---|---|---|
| `pydantic` (v2, current release) | runtime | Validated, frozen domain and event models. Product constraint. |
| `pytest-asyncio` | dev | Native async tests. Compatibility with pytest 9 is checked at install. If it fails, fall back to `asyncio.run` wrappers and record that. |
| `hypothesis` | dev | Property tests over event sequences, the fold and serialization. |

Exact resolved versions go in commit messages and the final report.

## 12. Commit sequence (each passes `make check`)
0. `docs: revise M1 runtime plan`: replace `docs/plans/m1-runtime-core.md` with this plan.
1. `feat(runtime): domain models and run state transitions`: `model.py` data types, event payloads, `RunStatus` and the transition table, exhaustive transition tests. Adds `pydantic`.
2. `feat(runtime): event envelope, serialization and state fold`: envelope, schema registry, `RunState`, `apply` and `fold`, Hypothesis tests. Adds `hypothesis`, the profile and the `.gitignore` entry.
3. `feat(runtime): RunStore port and in-memory adapter`: Protocol, errors, `load_run`, `InMemoryRunStore`, the contract suite. Adds `pytest-asyncio`.
4. `feat(adapters): SQLite run store with schema migration`: the contract suite is parametrized over both stores, plus SQLite-specific tests.
5. `feat(runtime): ModelProvider port and scripted fake provider`, with deduplication and counter tests.
6. `feat(runtime): single-turn runner`, with runner tests.
7. `test: crash recovery at every boundary`: wrappers, a 5-boundary × 2-store matrix, the subprocess test.
8. `test: enforce runtime import boundaries`, with checker self-tests.
9. `docs: accept ADR-0004/0005, add ADR-0006, update architecture docs`:
   - ADR-0004 accepted, revised: default journal mode instead of WAL, connection per operation, STRICT tables and triggers.
   - ADR-0005 accepted, revised: intent per attempt, fold strictness, OCC.
   - New ADR-0006: model-call execution semantics and the ambiguous outcome.
   - `overview.md`: state set, decided/open table.
   - `testing.md`: which properties are now covered.
   - `requirements.md`: FR-4 reworded to "never re-invoke after a durably recorded response; unrecorded outcomes are retried under the same operation id".
   - `roadmap.md`: M1 exit criteria; M1 marked done.
   - `CLAUDE.md`: repo map and current milestone.
   - ADRs are accepted only if commits 4–7 actually show the evidence ADR-0004 asks for. Otherwise they stay Proposed, with the gap stated.

## 13. Acceptance criteria / verification
- `make sync && make check` exits 0: ruff, pyright strict with 0 errors, all tests.
- Every legal transition succeeds, and every illegal one raises (exhaustive parametrization).
- Hypothesis properties pass: valid logs fold, mutated logs raise, serialization round-trips, store round-trips.
- The identical contract suite passes for `InMemoryRunStore` and `SqliteRunStore`.
- Concurrent appends at the same `expected_version`: exactly one succeeds.
- Crash matrix: each of the 5 boundaries × 2 stores reaches `completed`. Boundary 3 gives `generations == 1`, and boundary 4 gives 0 new invocations. The subprocess `SIGKILL` test passes.
- The architecture test passes on the real tree, and its self-test detects all the planted violations.
- Runtime dependencies are exactly `["pydantic…"]`. Nothing imports a vendor SDK.
- ADR statuses match the evidence, and ADR-0006 states plainly that there's no exactly-once guarantee.

## 14. Non-goals (M1)
Real providers, streaming, tools, multi-turn execution, budgets and step limits, cancellation, context assembly, memory and RAG, multi-agent, CLI or REST beyond `--version`, leases and fencing, snapshots, upcasters beyond the registry mechanism, WAL, encryption at rest, retry and backoff policy, listing or scanning incomplete runs.

## 15. Risks and open questions
- **pytest-asyncio and pytest 9 compatibility:** unverified until install. Fallback is in §11.
- **STRICT tables need SQLite 3.37 or newer.** The uv-managed CPython on CI should be newer, and `open()` fails loudly if not.
- **Hypothesis run time and flakiness with SQLite:** bounded `max_examples` for store properties, a derandomized CI profile.
- **The event schema may change in M2 and M3.** That's acceptable: events are versioned and nothing is released.
- **Open:** whether verbatim run input should be persisted long-term (retention and redaction policy), M2+.
- **Open:** who triggers recovery after a crash. In M1 it's an explicit `execute(run_id)`. A startup scan needs a `list_runs` store operation, deferred to M5 (CLI).
- **Open:** a limit on resume attempts to stop crash loops, with M2's retry policy.
- **Open:** recording the assembled request per call, with M4 context assembly.

## Implementation notes (deviations from this plan)

- **`IntegrityError` mapping simplified.** The append pre-checks run under the `BEGIN IMMEDIATE` write lock, so key violations can't reach the insert. Rather than mapping unreachable branches to contract errors, any database rejection during insert raises `StoreError`, and the whole batch rolls back (tested with a rejecting trigger).
- **Lint configuration:**
  - Ruff `N818` (exception names must end in `Error`) is disabled, because the contract uses domain names such as `ConcurrencyConflict`.
  - `ASYNC2xx` (blocking I/O inside coroutines) is exempted under `tests/` only.
- **pytest configuration:** `--import-mode=importlib` and `pythonpath = ["."]`, so nested test directories can share `tests/support/`.
- **`RunState` stores** status, version, run inputs, `operation_id`, `attempts`, `response` and `failure`, as planned. The fold additionally requires `ModelResponseRecorded.attempt` to equal the latest attempt.
- **Resolved versions:** pydantic 2.14.2 (runtime); hypothesis 6.170.14 and pytest-asyncio 1.4.1 (dev).
