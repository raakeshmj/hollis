# ADR-0021: Memory storage, retrieval and deletion semantics

- Status: Accepted (M4)
- Date: 2026-10-31

## Context

Memory must work locally and on-premises with no mandatory external services, behind a replaceable storage port. It needs:
- metadata and temporal filters;
- keyword relevance;
- relationships (supersession, conflicts, shared subjects);
- transactional updates;
- honest deletion.

The evidence available in 2026:
- **MemoryAgentBench:** BM25 far outperformed a long-context baseline on multi-query retrieval.
- **AMA-Bench:** memory systems over-rely on similarity retrieval and miss causal links.

Vector databases and graph stores are popular, but neither has been shown to help Hollis's workloads.

## Decision

**Storage.**
- `MemoryStore` port, with `InMemoryMemoryStore` (tests and ephemeral agents) and `SqliteMemoryStore` in its own database file.
- The SQLite schema:
  - `memories`: indexed columns for scope, slot, status, validity and dedup, plus the full record as JSON.
  - An FTS5 index over content, subject, key and tags (Porter stemming, Unicode tokenizer), kept in sync on every save and delete.
  - `memory_changes`: append-only change log; triggers refuse updates and deletes.
  - `memory_jobs`: idempotency markers.
- Same conventions as the run store: stdlib `sqlite3`, a connection per operation, `BEGIN IMMEDIATE`, mode 0600, versioned migrations.

**Retrieval** (`MemoryManager.recall`) is hybrid without embeddings:
- **Filtering:** only records valid and known at `as_of` (default now); retracted records never. Scopes are filtered in SQL and again in the manager.
- **Lexical:** BM25 (FTS5's built-in, or the same formula in memory), normalized to the best hit.
- **Subject:** a bonus only when the whole subject is named in the query, so generic words like "service" don't boost every match.
- **Entity links:** one hop from the three best matches, both to their subjects and to known subjects their content mentions ("owned by the Falcon team" reaches memories about "falcon team"), with a decayed inherited score.
- **Weights:** certainty, recency (30-day half-life), kind prior (preferences, procedures) and a bonus for active temporary conditions. Preferences are considered for every query.
- **Output:** conflicts annotated; packed under a result limit and a character budget; recall counts updated for usage-based retention.

The first evaluation run had multi-session evidence at 0%. The subject-bonus and entity-link rules came from that diagnosis, and a held-out scenario was written afterwards to check for overfitting (docs/architecture/evaluation.md).

**No embeddings, vector database or graph database in M4.**
- On our evaluation, lexical, metadata and entity-link retrieval gets the needed evidence into context for 100% of probes in the tuned scenario and 91% in the held-out one, at about 40% of the raw-history token cost.
- The remaining miss is a causal link through a machine identifier (`CUDA_ERROR_OUT_OF_MEMORY` → "CUDA out of memory"). Embeddings might bridge that; so might identifier normalization or a causal index. It's recorded as a known gap.
- An embedding scorer would need a gateway task family (embeddings, ADR-0011) and an evaluation showing benefit. That belongs with M5's retrieval work.

**Deletion semantics, stated honestly.**
- `forget` / `forget_subject`: the record and its indexed text are deleted, leaving a tombstone in the change log (id, scope, time, actor, reason, no content). `secure_delete` overwrites the freed pages in the database file.
- **Not erased by forgetting:**
  - copies in run logs: `MemoryRecalled` events of runs that recalled the memory, model responses and tool results that repeated it, and the source events it was derived from (ADR-0010 governs run deletion, still unimplemented);
  - artifacts;
  - backups and file-system copies;
  - the rollback journal during an active transaction.
- `retract` hides a memory from recall but keeps it in history (use it for "this was wrong" rather than "delete this").
- Retention deletion uses the same mechanism.

## Alternatives considered

- **A vector database or sqlite-vec:** a new dependency with no measured benefit here. The port allows adding a scorer later.
- **A graph database** for relationships: subject links, supersession and conflict links cover the measured multi-hop need in SQLite.
- **Postgres:** not local-first by default. A networked adapter can implement the port later.
- **External-content FTS** (index without a copy): saves space, but deletion and `secure_delete` reasoning gets subtler. The index holds its own copy and is deleted together with the record.

## Trade-offs

- **Paraphrase:** lexical retrieval misses paraphrases with no shared words. Agents can search again through `memory_recall` with their own query.
- **Entity-link noise:** links depend on subjects being set and named consistently. Short subjects (four characters or fewer) aren't linked, to limit noise.
- **Scale:** BM25 scores and recency weights are heuristics tuned on synthetic scenarios. Recall reads every distinct subject in the granted scopes, which is fine at the tested scale (hundreds to low thousands of records per scope) and unmeasured beyond it.

## Consequences

- **Evidence:**
  - `tests/memory/test_store_contract.py`, including FTS sync on delete, the append-only log, and deleted text absent from the file bytes.
  - `tests/memory/test_recall.py`, `tests/security/test_memory_security.py` (FTS syntax can't widen scope; a leaky backend is filtered), `tests/evals/`.
- **Requirements:** deletion of derived memories is implemented. Whole-run deletion is still policy only (ADR-0010), and the requirements say so.
