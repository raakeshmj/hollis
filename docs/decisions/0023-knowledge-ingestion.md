# ADR-0023: Knowledge ingestion, document structure and identifier-aware analysis

- Status: Accepted (M5)
- Date: 2026-11-01

## Context

External knowledge arrives in many formats, changes over time, and must be citable down to a location. Fixed-size character windows break tables, code and arguments, and lose where a passage came from. Technical text mixes prose with identifiers: error codes, versions, hostnames, symbols. M4's evaluation showed that a machine identifier (`CUDA_ERROR_OUT_OF_MEMORY`) didn't reach the prose that explains it ("CUDA out of memory").

## Decision

**Knowledge is separate from memory** (`hollis.knowledge`).
- It has its own store, lifecycle and access model.
- Ingestion never writes memory, and memory is never presented as a source (enforced by layering tests).

**Lifecycle of a source:**
1. **Identity:** (collection, key), with the key chosen by the caller (relative path, URL, record id).
2. **Fingerprints:** the content fingerprint is the sha256 of the raw bytes. The *index* fingerprint adds the extractor, chunking policy, analyzer version and embedding space. An unchanged index fingerprint and metadata means a no-op.
3. **Extraction:** content-sniffed (PDF and DOCX signatures, binary refusal) before any trust in the file name.
4. **Sanitization** (ADR-0025), then structure-aware chunking.
5. **Embedding:** in the collection's space, before the store is touched.
6. **Atomic replacement:** the source's sections, chunks, index entries and vectors are swapped in one transaction. An interruption leaves the previous version whole.

Versions increase on content change. Labels, trust and effective dates come only from the operator. `sync_directory` mirrors a tree: it doesn't follow symlinks, stays within the root, and deletes sources whose files are gone. `reconcile` finds and repairs orphaned index rows, stale vectors and missing vectors.

**Extraction**, pluggable by media type:

| Format | How it's handled |
|---|---|
| Markdown | ATX and setext headings, fenced code, pipe tables, lists, quotes; front matter only for the title |
| Plain text | Paragraphs with line ranges |
| HTML (stdlib parser) | Headings, paragraphs, lists, code, tables. Scripts, styles, comments and hidden elements dropped and counted |
| Source code | Python split at top-level definitions with `ast`; other languages at definition-like lines; line ranges kept |
| JSON | Top-level keys become sections with flattened `path: value` lines |
| CSV | Tables, with the header repeated |
| DOCX (stdlib zip and XML) | Heading styles, lists, tables. DTDs refused; size-limited |
| PDF (optional `pypdf` extra) | Text per page with page numbers |

OCR and multimodal extraction aren't included. Scanned PDFs yield no text and are reported via `pages_without_text`. The location model (page, line, section path) leaves room for visual retrieval later.

**Chunking:**
- Every heading starts a section; sections form a tree (parent-child expansion).
- Cuts fall between blocks.
- Oversized code splits at line boundaries, oversized tables by rows with the header repeated, oversized prose at sentence boundaries with a small overlap.
- Sizes use the runtime's token estimate.
- Each chunk keeps its section path, location, source version, content hash and instruction flags.
- What gets embedded is the document title, section breadcrumb and text (contextual chunk headers).

**Identifier-aware analysis** (`hollis.runtime.text`, shared by knowledge, memory and the hashing embedder):
- Tokens keep `. - / : @ _` joints whole (versions, hostnames, paths, symbols) and also emit their parts, including camelCase and letter-digit boundaries.
- The FTS index gets the parts. An exact index gets the whole identifiers.
- In M4 memory, entity links also match when every content word of a subject appears among a memory's analyzed tokens. This closed the M4 gap; its strict `xfail` is now a regression test.

## Alternatives considered

- **Unstructured, Docling, LlamaParse-style parsers:** better on complex layouts, but heavy (models, system libraries) or cloud-based. The extractor registry lets an operator plug one in.
- **Fixed-size chunks with overlap:** simple, but they break structure, and locations become approximate.
- **Semantic (embedding-based) chunking:** needs a model at ingestion time. Structure-aware boundaries are deterministic and evaluated.
- **Trusting front matter or document metadata for access control or trust:** that would let a document grant itself access.

## Trade-offs

- PDF extraction is plain text per page. Layout, tables and reading order in complex PDFs are only as good as `pypdf`'s text layer. There's no OCR.
- Generic code splitting by definition-like lines is heuristic outside Python.
- Re-ingesting a source whose governance metadata changed re-chunks and re-embeds it, which costs extra but keeps one code path.

## Consequences

- **Evidence:** `tests/knowledge/test_extract.py` (every format, located blocks, hidden content, sanitization, flags, chunk boundaries) and `test_lifecycle.py` (idempotency, atomic replacement, sync, deletion, migration, reconcile, redaction, limits).
- **New dependency:** `pypdf` (optional extra `pdf`, BSD-3, pure Python).
