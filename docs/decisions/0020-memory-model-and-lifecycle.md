# ADR-0020: Memory model, lifecycle and scope

- Status: Accepted (M4)
- Date: 2026-10-31

## Context

Agents need experience across separate runs: user preferences and constraints, project facts, what worked or failed, decisions, past tasks. Hollis serves coding, research, automation and custom agents, so memory semantics can't assume one specialization.

The research (docs/plans/m4-context-memory.md):
- Benchmarks separate retrieval, test-time learning, long-range understanding, knowledge updates, temporal reasoning, abstention and selective forgetting (LongMemEval, MemoryAgentBench).
- Agent memory is mostly machine-generated interaction logs (AMA-Bench).
- Production systems decide explicitly between add, update and delete, and track when facts held.

Memory is also long-term retention: a privacy, poisoning and leakage surface (OWASP ASI06, ASI03).

## Decision

**One record type with typed fields**, not one store per cognitive category:
- **`kind`:** fact, preference, procedure, episode, decision, observation. Working memory is the run's own context (ADR-0018). Session memory is a scope, not a type.
- **`certainty`:** inferred < observed < confirmed. The *source* caps it, not the content: models write `inferred`; tools and system `observed`; people and operators `confirmed`; anything from external content at most `observed`.
- **`subject` + `key`:** a *slot*. This is how Hollis tells changes apart without guessing from wording.
- **`valid_from` / `valid_until`** (when it holds) and **`recorded_at`** (when Hollis learned it). Queries "as of" a time see what was valid and known then.
- **`status`:** active, superseded, retracted, expired.
- **`provenance`:** source kind, actor, run and reference, and an external flag. Plus conflict and supersession links, and a dedup key.

**Governed writes.** Nothing is memorized automatically. A write happens only through:
- the application (`MemoryManager.remember`);
- a model through `memory_remember`, which is a mutating tool, so M3 policy applies: approval by default, attributed to the invocation, idempotent per invocation;
- the deterministic episode recorder;
- the model-assisted extractor.

Every write passes the same pipeline:
1. Scope access.
2. Policy: kinds per source, size, registered secrets and credential patterns refused, certainty capped.
3. Dedup key: repeats are a no-op.
4. Exact duplicate (normalized content, slot and kind): reinforced, possibly upgraded in certainty.
5. Slot decision:

| Writer says | Slot empty | Slot holds a different value |
|---|---|---|
| `new` | created | **conflict**: both kept, linked both ways, shown together |
| `update` | created | old **superseded**, its validity ends now; the past stays queryable |
| `correction` | created | old **retracted**: never recalled, kept in history |
| `temporary` (needs `valid_until`) | created | stored alongside; the base value applies again after expiry |

A lower-certainty update or correction never replaces a higher-certainty value; it becomes a conflict. Writes are serialized in-process, and each is saved atomically with its change-log entries.

**Scope and isolation.**
- Memory lives in namespaces (`kind:id`, e.g. `user:alice`, `agent:researcher`, `workspace:acme/billing`, `session:42`).
- A run is granted `MemoryAccess(read, write)` by the application in `RunSpec.memory`. The grant is recorded in `RunCreated` (so recovery uses the same grant), passed to tools in `ToolContext.memory`, and enforced at three levels:
  - store queries;
  - the manager;
  - the runner and the fold (`MemoryRecalled` outside the grant is rejected).
- A model can name a scope only within the grant. Hollis has no authentication, and the namespaces are the access boundary the host application maps its identities onto.
- Memory never feeds tool policy: recalled memories can't grant permissions.

**Recall into runs.**
- Once, at run start, recorded in `MemoryRecalled`. It stays stable for the run, which keeps the provider's cache prefix stable.
- On demand through `memory_recall` (read-only), the tool-augmented retrieval AMA-Bench found effective.
- Recalled memories are rendered with their kind, certainty, dates, source, external flag and conflicts, as framed data.

**Learning from runs.**
- `episode_candidate` is deterministic from the event-derived state: task, outcome, tools, side effects, answer. Idempotent per run.
- `MemoryExtractor` is model-assisted through the `ModelProvider` port. Its proposals are `inferred` and go through the same pipeline. Completion is recorded as a job, so re-running after completion is a no-op. After a crash midway, content-derived dedup keys prevent identical proposals from being stored twice. Invalid replies store nothing and are reported.

**Maintenance is conservative.**
- Expire ended temporary memories.
- Delete unconfirmed memories of kinds with a maximum age (observations 30 days, episodes 180) when they're old *and* not recalled within that period.
- Optionally delete superseded or retracted history after `history_retention_days`.
- Confirmed memories are never deleted by maintenance. No global model-driven consolidation.

## Alternatives considered

- **Separate stores per memory type** (episodic, semantic, procedural): more code with no demonstrated benefit. `kind` captures the distinction.
- **Inferring updates and contradictions from similarity:** silently overwrites ("server A down" vs "server B down"). Explicit slots are reliable.
- **Automatic memorization of every message:** grows without bound and memorizes injected instructions and secrets.
- **Last-write-wins:** loses corrections' history and lets weaker claims overwrite confirmed ones.
- **Model-driven consolidation jobs:** an unreliable summary could delete good evidence. Deferred until an evaluation shows a need.

## Trade-offs

- Writers must supply subject and key to get update and correction semantics. Without a slot, a statement is independent and only exact duplicates are merged; paraphrases accumulate.
- Model-proposed memories need approval by default, which adds friction until an operator allows them with a rule.
- **Copies on purpose:** episodes and extraction copy run content into memory. The application opts into this duplication.
- **Single writer:** concurrent writers in separate processes could interleave slot decisions. Same assumption as the run store (ADR-0004).

## Consequences

- **Evidence:**
  - `tests/memory/` (lifecycle, store contract on both adapters, recall, runtime integration).
  - `tests/security/test_memory_security.py`.
  - `tests/evals/` (docs/architecture/evaluation.md).
  - Mutation checks cover store, manager and runner scope filters, write access, secret and certainty policy, conflict-instead-of-overwrite, and retention of confirmed memories.
- **Not verified:** model-assisted extraction and summarization quality with real models.
