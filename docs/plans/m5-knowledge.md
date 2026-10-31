# M5 plan: knowledge retrieval and retrieval-augmented generation

Branch `m5-knowledge`, from `m4-context-memory`. Owned end to end. No separate approval step.

## Starting point (M4)

- **Context engine:** recorded plans, budgets, compaction, artifacts.
- **Memory:** governed, scoped, temporal; SQLite FTS5 with subject, entity-link and temporal recall.
- **No embeddings:** the gateway serves text generation only.
- **Known gap:** an identifier in a log (`CUDA_ERROR_OUT_OF_MEMORY`) doesn't retrieve the procedure written as "CUDA out of memory".

## Live verification status (2026-11-01)

No inference or embedding model is available: Ollama has no models, and no API keys are set. Retrieval mechanics, exact and lexical quality, fusion behaviour, lifecycle and security are validated offline.

Semantic (dense) quality is validated only through an opt-in live evaluation, which hasn't been run. Offline dense results come from two local embedders:
- a real character-n-gram hashing embedder (no model);
- a clearly labelled *simulated* semantic embedder that tests fusion mechanics. It is not evidence of real semantic quality.

## Research (primary sources)

- **OWASP RAG Security Cheat Sheet:**
  - Filter before retrieval, with permission metadata on every chunk enforced at query time. Fail closed.
  - Propagate deletions and revocations to chunks, indexes and caches.
  - Scan for invisible Unicode and injection markers; delimit retrieved text as data; cap chunks and tokens.
  - Record provenance and hashes. Treat embeddings as sensitive as their text.
  - Audit document ids and decisions without logging raw content.
- **AgenticRAG** (Microsoft Research, 2026): a reasoning model with search, find, open and summarize tools over existing search. Moving from single-shot retrieval to agentic tool use was the largest factor (5.9× in ablation), and in-document navigation and multi-query search helped. No cost figures. → Hollis exposes search, find, open and read as M3 tools and keeps single-pass retrieval for simple questions.
- **M4 evaluation and MemoryAgentBench:** BM25 is a strong baseline. Similarity-only retrieval misses causal links (AMA-Bench). → Dense retrieval complements lexical retrieval but doesn't replace it, and exact identifiers must stay discoverable.

## Design

1. **Embeddings task family in the gateway.**
   - `Task.EMBEDDING`, `EmbeddingCapabilities` (dimensions, batch size, input limit, normalization), and an `EmbeddingProvider` port with a request type that distinguishes query from document.
   - Adapter: OpenAI-compatible `/v1/embeddings` (OpenAI, Ollama, vLLM, llama.cpp, LM Studio, TEI, Infinity), over raw httpx2, like M2.
   - Local adapter: a character-n-gram hashing embedder, needing no model or network.
   - `ModelGateway.embedder(id)`. Embedding *spaces* (deployment, model, dimensions, version) are tracked, and vectors from different spaces are never mixed.
2. **Knowledge is separate from memory** (`hollis.knowledge`): its own store, lifecycle, access model and context kind. It never writes memory.
3. **Ingestion lifecycle:**
   - source identity, then content fingerprint, extraction (pluggable by media type), sanitization, structure-aware chunking, embedding, and an atomic version swap;
   - re-ingesting unchanged content is a no-op;
   - directory sync deletes sources whose files are gone;
   - formats: Markdown, text, HTML, source code, JSON and CSV, DOCX (stdlib), PDF (optional `pypdf` extra);
   - OCR and multimodal are deferred, but the location model (page, line, section path) leaves room for them.
4. **Chunking:**
   - Heading-aware sections, cut at block boundaries.
   - Tables and code kept whole where possible, with headers repeated when split.
   - Sections serve as parents (parent-child expansion).
   - Every chunk carries its source, version, section path, location and hash.
5. **Storage:** SQLite (own file):
   - sources, sections and chunks;
   - FTS5 over identifier-analyzed text (identifiers indexed whole *and* split into sub-tokens);
   - float32 vectors per embedding space, searched by exact brute force;
   - an append-only change log; `secure_delete` on.
   - No vector database (scale and latency measured and documented). A vector-index interface is left open for later.
6. **Retrieval pipeline:**
   - query analysis (identifiers, quoted phrases, multi-part questions);
   - lexical, dense and exact retrievers, with access filtering in SQL before scoring;
   - fusion: RRF and weighted fusion compared, plus an exact-identifier guarantee;
   - optional deterministic reranker; deduplication; parent-section expansion;
   - decomposition for multi-part questions only.
   - The configuration is chosen by evaluation.
7. **Access control:** `KnowledgeAccess(collections, labels)` per run, recorded in `RunCreated` and enforced in SQL, the retriever, the runner and the fold.
   - Source ACL labels are set by the ingesting operator, never by document content.
   - Permission changes take effect on the next query (no caches).
8. **RAG in the context engine:**
   - `KnowledgeRetrieved` is recorded at run start; passages carry stable citation keys, source identity, version, location and trust. They're rendered as source documents, distinct from memories.
   - They're optional context: dropped first under pressure and reported.
   - Knowledge tools (search, open an outline, read a range, find a term, source metadata) are read-only M3 tools for iterative retrieval.
9. **Grounding:** after the final answer, a deterministic check records `AnswerGrounded`:
   - citations parsed and resolved to passages, with unknown keys flagged as fabricated;
   - claims without citations, and lexical support of cited passages;
   - abstention detected.
   It's a measurement, not proof of correctness.
10. **Security:**
    - sanitization (invisible and bidi characters, hidden HTML, script and style), with counts recorded;
    - instruction-pattern flags, with flagged chunks excluded from automatic context by default;
    - ingestion limits; operator-only ingestion (no model ingestion tool);
    - Hollis-assigned citation identities;
    - an audit log without content.
11. **Evaluation:**
    - A deterministic corpus covering exact identifiers, error codes, versions, paraphrase, synonyms, multi-hop and cross-document questions, temporal and conflicting sources, unanswerable questions and distractors, plus the CUDA regression case. A held-out corpus is written after tuning.
    - Retrieval metrics (Recall@k, Precision@k, MRR, nDCG, evidence coverage), latency, index time and storage, compared across configurations.
    - Grounding-verifier accuracy on labelled answers.
    - Opt-in live evaluation for real embeddings and real answers.

## Out of scope

Web crawling and remote connectors (the source interface leaves room for them), OCR and multimodal, graph RAG, cross-encoder reranking as a gateway task (interface only), ANN indexes, and model-driven autonomous research loops.
