# ADR-0024: Knowledge storage, hybrid retrieval and access control

- Status: Accepted (M5)
- Date: 2026-11-01

## Context

Hollis must retrieve evidence across collections that may be local, private or cloud-hosted. Retrieval must be permission-aware (OWASP: filter before retrieval, fail closed, enforce at query time) and must preserve exact matches. It should work on one machine without external services. M4 established SQLite and FTS5. The question was whether dense retrieval, a vector database or graph retrieval earn their place.

## Decision

**Storage.**
- A `KnowledgeStore` port, with `SqliteKnowledgeStore` in its own file:
  - collections, sources (plus a label table), sections and chunks;
  - FTS5 (Porter stemming, Unicode) over chunk text with identifier parts and section breadcrumbs;
  - an exact identifier index;
  - float32 vectors per embedding space, searched by exact scan;
  - an append-only change log; `secure_delete`.
- **No vector database.** Measured exact dense scan at 384 dimensions, in pure Python on an Apple M3 Pro:

  | Chunks | Dense p50 | Lexical p50 | Database size |
  |---|---|---|---|
  | 1,000 | 12 ms | 0.4 ms | 2.7 MB |
  | 5,000 | 57 ms | 0.7 ms | 13 MB |
  | 20,000 | 242 ms | 1.3 ms | 53 MB |

  Fine for local and team-sized collections. Beyond roughly 10,000 chunks per query scope, an approximate index (sqlite-vec, pgvector, a dedicated store) belongs behind the same port.

**Access control in the store.**
- Every read takes a `KnowledgeAccess` (collections, labels) and applies it inside the SQL, so restricted chunks are never scored, ranked or returned.
- A source with labels is visible only to holders of one of them. An empty grant matches nothing.
- Label changes take effect on the next query; there are no caches.
- The grant is recorded in `RunCreated`, re-checked by the runner, and enforced by the fold: retrieved passages outside the grant are rejected.

**Retrieval** (`KnowledgeRetriever`, the runtime's `KnowledgeRetrieval` port):
1. Requested collections intersected with the grant.
2. Concurrent retrievers, each limited to visible chunks:
   - lexical BM25 over identifier-analyzed terms;
   - dense (only for collections in the embedder's space);
   - exact identifier.
3. **Reciprocal rank fusion** (k = 60), with an **exact-first** guarantee: chunks containing every query identifier rank first.
4. A **follow-up hop** for bridging questions: words adjacent to query terms in the best first-hop chunk ("payments" in "owned by the payments service") are searched with the query's terms, and the results interleaved.
5. De-duplication and a per-source cap.
6. Decomposition only for multi-question input.
7. Packing under passage and character limits with Hollis-assigned citation keys. Passages of deleted or re-versioned sources are never packed.

Optional: coverage reranking, parent-section expansion, weighted fusion.

**Defaults chosen by evaluation** ([rag-evaluation.md](../architecture/rag-evaluation.md)):
- Multi-hop stayed at 0.5 Recall@5 under every similarity configuration, simulated semantics included. The follow-up hop raised it to 1.0 and became a default (adds under 1 ms).
- Dense-only retrieval was worse than hybrid on both corpora. Hybrid was never worse than lexical on Recall@5.
- Weighted fusion and reranking didn't improve the development corpus. Reranking improved MRR on the held-out one, so it's an option, not a default.
- Exact-first is decisive under weighted fusion. Under RRF, the exact retriever's own vote already sufficed in practice.

**Into runs.**
- With `RunSpec.knowledge` and `Runner(knowledge=…)`, passages are retrieved once at run start and recorded (`KnowledgeRetrieved`), excluding flagged chunks by default.
- They're rendered as `knowledge` context blocks, distinct from memories, with key, source, version, location, effective date and trust.
- They're optional context, dropped before memories under pressure.

**Iterative retrieval** with read-only M3 tools: `knowledge_search`, `knowledge_list`, `knowledge_outline`, `knowledge_read` and `knowledge_find`. They return passages with citation handles, so an agent can navigate large documents without loading them. That's the AgenticRAG pattern without a second execution engine, bounded by tool limits and policy.

## Alternatives considered

- **A vector database by default:** an operational dependency without measured benefit at this scale.
- **Dense-only retrieval:** measurably worse, and it buries identifiers.
- **Graph RAG:** the measured multi-hop need was met by the follow-up hop. Revisit with evidence of relation-heavy workloads.
- **LLM query rewriting and multi-query by default:** a model call on every query. Simple questions shouldn't pay for it; agents can search again through tools.
- **Post-retrieval permission filtering:** weaker; OWASP recommends filtering before retrieval.

## Trade-offs

- Exact scan is linear in visible chunks.
- The follow-up hop is a lexical heuristic: it helps bridging questions phrased like the evaluation's, and it adds candidates that may be irrelevant to simple questions (they come after the first-hop leaders).
- Labels are coarse (set membership). Attribute-based policies would need a richer model.
- Real dense quality is unmeasured: offline dense numbers come from the hashing embedder or a simulated one.

## Consequences

- **Evidence:**
  - `tests/knowledge/test_retrieval.py`, `test_rag_runtime.py`, `tests/security/test_knowledge_security.py`, `tests/evals/rag/`.
  - Mutation checks: removing the collection filter, label filter, grant intersection, exact-first, space check, follow-up, runner filter or fold visibility rule fails tests.
- **Future:** an approximate vector index adapter, cross-encoder reranking as a gateway task, web and enterprise connectors.
