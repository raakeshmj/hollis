# Knowledge retrieval and grounding evaluation

What M5's evaluation measures, how, and what it produced. Regenerate the tables with `uv run python -m tests.evals.rag.report`. `tests/evals/rag/test_rag_eval.py` asserts thresholds below these results, so regressions fail `make check`.

## What is and is not validated

| Validated offline (deterministic) | Not validated (no model or server available) |
|---|---|
| Retrieval mechanics, access control, lifecycle | Dense retrieval with a real embedding model |
| Lexical, exact and identifier retrieval quality | Answer correctness and citation behaviour of real models |
| Fusion behaviour with the local hashing embedder | Any embedding or inference server's compatibility |
| Fusion with a **simulated** semantic embedder (mechanics only) | |
| The grounding verifier against labelled answers | |

**Simulated semantic embedder.** It maps hand-written synonym groups, chosen by the evaluation author *while knowing the queries*, onto the hashing embedder. It shows how fusion behaves when a dense model captures synonyms, and whether exact identifiers survive a strong dense signal. Its numbers are **not** evidence of any real model's quality.

The opt-in live evaluation (`tests/evals/rag/test_live_rag_eval.py`, `HOLLIS_EVAL_*`) measures real embeddings and real answers through the gateway. It hasn't been run: Ollama is installed on this machine with no models, and no API keys are set.

## Method

**Corpora** (`tests/evals/rag/corpus.py`):
- **`engineering`** (development: configuration choices were made on it).
  - 14 mixed-format documents: Markdown runbooks, an HTML API guide, Python code, JSON configuration, a CSV code table, a DOCX policy, a text changelog, a machine log line, dated and conflicting policies.
  - 30 seeded near-duplicate runbooks as distractors, and 5 hard negatives that share surface words with the paraphrase and synonym queries.
- **`clinical_ops`** (held out: written after the defaults were chosen, never tuned against). 9 documents and 30 distractors in a different domain.

**Relevance** is defined by fact phrases, not chunk ids, so labels survive chunking changes:
- grade 2 for a chunk with a primary fact, grade 1 for a supporting (bridging) fact;
- Recall@5 counts the required facts present in the top 5.

**Queries:**
- From the development corpus: exact identifiers, error codes, versions, paraphrases, synonyms (including one with zero lexical overlap), multi-hop (bridge plus answer), cross-document, temporal (dated versions), conflicting sources, distractor-heavy and unanswerable.
- The **M4 regression case**: a log's `CUDA_ERROR_OUT_OF_MEMORY` must reach the runbook text "CUDA ran out of memory … halve the batch size".

**Metrics:** Recall@5, Precision@5, MRR, nDCG@10, and exact@1 (identifier queries whose top chunk holds the primary fact), plus p50 and p95 latency, index build time, index size and embedding calls. Unanswerable queries are excluded from retrieval metrics; their behaviour is a generation property, tested live.

## Results (2026-11-01, offline, SQLite, Apple M3 Pro)

### engineering-30

| configuration | Recall@5 | Precision@5 | MRR | nDCG@10 | exact@1 | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|
| lexical (M4-style terms) | 0.90 | 0.24 | 0.77 | 0.72 | 0.80 | 0.4 | 1.6 |
| lexical (identifier-aware) | 0.90 | 0.24 | 0.77 | 0.74 | 0.80 | 0.4 | 1.7 |
| lexical + exact | 0.90 | 0.24 | 0.81 | 0.76 | 1.00 | 0.4 | 1.9 |
| lexical + exact + follow-up | 0.93 | 0.25 | 0.81 | 0.79 | 1.00 | 0.7 | 2.4 |
| dense (hashing) | 0.90 | 0.24 | 0.82 | 0.79 | 0.80 | 2.1 | 3.3 |
| hybrid rrf (hashing) | 0.90 | 0.24 | 0.84 | 0.81 | 0.80 | 2.6 | 3.0 |
| hybrid rrf + exact-first (hashing) | 0.90 | 0.24 | 0.84 | 0.81 | 0.80 | 2.5 | 4.0 |
| hybrid weighted + exact-first (hashing) | 0.90 | 0.24 | 0.79 | 0.76 | 0.80 | 2.6 | 4.0 |
| hybrid rrf + exact-first + rerank (hashing) | 0.90 | 0.24 | 0.84 | 0.81 | 0.80 | 4.0 | 5.8 |
| DEFAULT: hybrid rrf + exact-first + follow-up (hashing) | 1.00 | 0.27 | 0.85 | 0.84 | 0.80 | 2.8 | 4.7 |
| dense (SIMULATED semantic) | 0.97 | 0.25 | 1.00 | 0.93 | 1.00 | 2.3 | 3.6 |
| hybrid rrf + exact-first (SIMULATED semantic) | 0.97 | 0.25 | 0.97 | 0.89 | 1.00 | 2.6 | 4.3 |
| DEFAULT config (SIMULATED semantic) | 1.00 | 0.27 | 0.97 | 0.92 | 1.00 | 3.0 | 5.0 |

Recall@5 by category:

| configuration | exact_identifier | error_code | version | paraphrase | synonym | identifier_to_concept | multi_hop | cross_document | temporal | conflicting | distractor |
|---|---|---|---|---|---|---|---|---|---|---|---|
| lexical (M4-style terms) | 1.00 | 1.00 | 1.00 | 1.00 | 0.50 | 1.00 | 0.50 | 1.00 | 1.00 | 1.00 | 1.00 |
| lexical (identifier-aware) | 1.00 | 1.00 | 1.00 | 1.00 | 0.50 | 1.00 | 0.50 | 1.00 | 1.00 | 1.00 | 1.00 |
| lexical + exact | 1.00 | 1.00 | 1.00 | 1.00 | 0.50 | 1.00 | 0.50 | 1.00 | 1.00 | 1.00 | 1.00 |
| lexical + exact + follow-up | 1.00 | 1.00 | 1.00 | 1.00 | 0.50 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| dense (hashing) | 1.00 | 1.00 | 1.00 | 0.50 | 1.00 | 1.00 | 0.50 | 1.00 | 1.00 | 1.00 | 1.00 |
| hybrid rrf (hashing) | 1.00 | 1.00 | 1.00 | 0.50 | 1.00 | 1.00 | 0.50 | 1.00 | 1.00 | 1.00 | 1.00 |
| hybrid rrf + exact-first (hashing) | 1.00 | 1.00 | 1.00 | 0.50 | 1.00 | 1.00 | 0.50 | 1.00 | 1.00 | 1.00 | 1.00 |
| hybrid weighted + exact-first (hashing) | 1.00 | 1.00 | 1.00 | 0.50 | 1.00 | 1.00 | 0.50 | 1.00 | 1.00 | 1.00 | 1.00 |
| hybrid rrf + exact-first + rerank (hashing) | 1.00 | 1.00 | 1.00 | 0.50 | 1.00 | 1.00 | 0.50 | 1.00 | 1.00 | 1.00 | 1.00 |
| DEFAULT: hybrid rrf + exact-first + follow-up (hashing) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| dense (SIMULATED semantic) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.50 | 1.00 | 1.00 | 1.00 | 1.00 |
| hybrid rrf + exact-first (SIMULATED semantic) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 0.50 | 1.00 | 1.00 | 1.00 | 1.00 |
| DEFAULT config (SIMULATED semantic) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |

| index | build s | chunks | size KiB | embedding calls |
|---|---|---|---|---|
| none | 0.06 | 85 | 316 | 0 |
| hashing | 0.09 | 85 | 664 | 49 |
| simulated | 0.10 | 85 | 664 | 49 |

### clinical-ops-30

| configuration | Recall@5 | Precision@5 | MRR | nDCG@10 | exact@1 | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|
| lexical (M4-style terms) | 1.00 | 0.26 | 0.93 | 0.89 | 1.00 | 0.4 | 1.3 |
| lexical (identifier-aware) | 1.00 | 0.26 | 0.93 | 0.89 | 1.00 | 0.5 | 1.2 |
| lexical + exact | 1.00 | 0.26 | 0.93 | 0.89 | 1.00 | 0.4 | 1.3 |
| lexical + exact + follow-up | 1.00 | 0.26 | 0.93 | 0.89 | 1.00 | 0.8 | 1.8 |
| dense (hashing) | 0.79 | 0.20 | 0.88 | 0.77 | 1.00 | 1.9 | 2.6 |
| hybrid rrf (hashing) | 1.00 | 0.26 | 0.93 | 0.89 | 1.00 | 2.2 | 2.6 |
| hybrid rrf + exact-first (hashing) | 1.00 | 0.26 | 0.93 | 0.89 | 1.00 | 2.2 | 3.2 |
| hybrid weighted + exact-first (hashing) | 1.00 | 0.26 | 0.93 | 0.89 | 1.00 | 2.2 | 3.4 |
| hybrid rrf + exact-first + rerank (hashing) | 1.00 | 0.26 | 1.00 | 0.94 | 1.00 | 3.3 | 4.4 |
| DEFAULT: hybrid rrf + exact-first + follow-up (hashing) | 1.00 | 0.26 | 0.93 | 0.89 | 1.00 | 2.6 | 3.9 |
| dense (SIMULATED semantic) | 0.79 | 0.20 | 1.00 | 0.87 | 1.00 | 2.1 | 2.7 |
| hybrid rrf + exact-first (SIMULATED semantic) | 1.00 | 0.26 | 0.93 | 0.92 | 1.00 | 2.4 | 3.4 |
| DEFAULT config (SIMULATED semantic) | 1.00 | 0.26 | 0.93 | 0.92 | 1.00 | 2.7 | 3.9 |

Recall@5 by category:

| configuration | error_code | version | paraphrase | synonym | identifier_to_concept | multi_hop | cross_document |
|---|---|---|---|---|---|---|---|
| lexical (M4-style terms) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| lexical (identifier-aware) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| lexical + exact | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| lexical + exact + follow-up | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| dense (hashing) | 1.00 | 1.00 | 0.00 | 1.00 | 1.00 | 0.50 | 1.00 |
| hybrid rrf (hashing) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| hybrid rrf + exact-first (hashing) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| hybrid weighted + exact-first (hashing) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| hybrid rrf + exact-first + rerank (hashing) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| DEFAULT: hybrid rrf + exact-first + follow-up (hashing) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| dense (SIMULATED semantic) | 1.00 | 1.00 | 1.00 | 1.00 | 0.00 | 0.50 | 1.00 |
| hybrid rrf + exact-first (SIMULATED semantic) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| DEFAULT config (SIMULATED semantic) | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |

| index | build s | chunks | size KiB | embedding calls |
|---|---|---|---|---|
| none | 0.05 | 69 | 268 | 0 |
| hashing | 0.07 | 69 | 552 | 39 |
| simulated | 0.07 | 69 | 552 | 39 |

### Grounding verifier: 16/16 claims classified as labelled

| expected | got | count |
|---|---|---|
| fabricated | fabricated | 2 |
| supported | supported | 10 |
| uncited | uncited | 1 |
| weak | weak | 3 |

## What the results show

- **The default** is hybrid RRF with lexical, dense and exact retrieval, exact-first and the follow-up hop. It reaches Recall@5 1.00 on both corpora, with nDCG@10 0.84 (development) against 0.72 for M4-style lexical retrieval. Among configurations using a real local embedder (hashing), it's the only one with full recall in every development category; the same configuration with the simulated embedder also has it.
- **Multi-hop needed a structural change, not better similarity.**
  - Bridging questions stayed at 0.5 Recall@5 in every configuration, including the simulated semantic embedder.
  - The follow-up hop takes words adjacent to query terms in the best first-hop chunk ("payments" in "owned by the payments service") and searches them with the query. That raised multi-hop to 1.0.
  - Its interleaving also recovered a paraphrase that hashing dense retrieval had buried.
- **Dense alone is not enough.** Hashing dense retrieval alone lost recall on the held-out corpus (0.79), failing the paraphrase probe and half of the multi-hop probe. Simulated dense alone also missed identifier and multi-hop probes there. Hybrid was never worse than lexical on Recall@5.
- **Exact matches survive fusion.**
  - Identifier, error-code and version recall is 1.00 for every configuration with exact-first.
  - exact@1 is 0.80 for the hashing default because `MAX_RETRIES` appears in two chunks. The docstring chunk that mentions it ranks above the assignment the label calls primary. Both contain the identifier, so this is a ranking among exact matches, not a lost match.
  - A unit test shows exact-first is decisive under weighted fusion, where normalized lexical and dense scores can outvote the exact retriever.
- **Hashing dense retrieval helps and hurts.** It found the zero-overlap synonym ("rideshare" vs "ride-hailing", through character n-grams), but on its own it buried a paraphrase under a hard negative. This is the surface-similarity limit ADR-0022 describes.
- **Reranking and weighted fusion** didn't improve the development corpus. Reranking raised MRR on the held-out corpus (0.93 to 1.00). It stays optional.
- **The held-out corpus turned out easy.** Lexical retrieval alone already reaches Recall@5 1.00, so it confirms no regression but doesn't discriminate between hybrid configurations.
- **Cost:** hybrid queries take about 3 ms p50 at these sizes. Measured scale for exact dense scan is in ADR-0024 (242 ms at 20,000 chunks of 384 dimensions). Indexing these corpora takes under 0.1 s with the hashing embedder; real embedding models will dominate ingestion time.
- **Grounding verifier:** it agrees with every labelled claim. The labels were written by the verifier's author, so this shows the rules behave as designed, not that they generalize. A labelled negation case documents the known blind spot: "reimbursed" vs "not reimbursed" counts as supported.

## Limitations

- **Synthetic corpora written by the implementer.** No public benchmark (BEIR, BRIGHT, FinanceBench, ViDoRe) was run: they need model access and downloads. Running one with a real embedding model is the most important next measurement.
- **No real semantic signal.** Dense results come from the hashing or simulated embedders only.
- **Small corpora.** Collections are tens of documents. Scale was measured separately for latency only.
- **Unanswerable and conflicting questions** are measured as retrieval (both conflicting sources retrieved). Whether a model abstains or reports the conflict requires the live evaluation.
