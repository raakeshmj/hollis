# ADR-0025: RAG grounding, citations, trust and deletion

- Status: Accepted (M5)
- Date: 2026-11-01

## Context

Retrieved documents are untrusted. They can carry indirect prompt injection, hidden text, misleading claims and forged attributions. A retrieved passage isn't proof that an answer is correct, and models can cite sources they never saw. Deletion must be honest about which copies remain. Primary reference: the OWASP RAG Security Cheat Sheet.

## Decision

**Untrusted content handling at ingestion:**
- Invisible, bidi, tag and control characters are removed and counted.
- Hidden HTML (`hidden`, `aria-hidden`, `display:none`, `visibility:hidden`, zero size or opacity), scripts, styles and comments are dropped and counted.
- DOCX DTDs are refused.
- Chunks with instruction-like patterns (override, role markers, persona, secrecy, exfiltration, tool commands) are **flagged**, not deleted, since legitimate documents discuss injection:
  - Flagged chunks are excluded from automatic run-start context by default (`knowledge_include_flagged`).
  - Tools show them with a warning.
- Registered secrets are redacted before indexing.
- Ingestion is an operator API with limits. There's no model-facing ingestion or deletion tool.

**Trust and authority:**
- `SourceTrust` (curated, internal, external) and access labels are set by the operator, never by document content. Front matter can set only a title.
- Passages are framed as data, with source, version, location, effective date and trust.
- Retrieved content never reaches authorization: tool policy doesn't read it, and a poisoned document can't trigger a tool (security test).

**Citations:**
- Hollis assigns citation keys: `S1..Sn` for run-start passages, and `C-xxxxxxxx` handles derived from chunk ids for tool results.
- Handle-like text inside documents is neutralized, so a document can't forge a citation.
- Evidence handles are accepted only from *trusted* tools, so an external tool can't print fake evidence (security test).

**Grounding** (`hollis.runtime.grounding`): after a final answer, if the run had knowledge evidence, a deterministic check records `AnswerGrounded`:
- per claim, `supported`, `weak`, `uncited` or `fabricated`;
- the cited and fabricated keys;
- whether the answer abstained.

It's lexical. It measures whether citations point at evidence that shares the claim's content, not whether the claim is true. Negation isn't detected; "reimbursed" vs "not reimbursed" scores as supported, and this is documented and tested. It doesn't block answers; applications decide what to do with the report.

**Conflicts and dates:**
- Conflicting sources are both retrievable (evaluated) and rendered with versions, effective dates and trust, so the model, and a reader, can see the disagreement.
- Sources with a future effective date are excluded as of the query time.
- Hollis doesn't resolve conflicts automatically.

**Deletion, stated honestly.** `delete_source` and `delete_collection` remove, in one transaction:
- the source's chunks, full-text entries, identifier entries, vectors, sections and labels;
- leaving a content-free entry in the change log;
- `secure_delete` overwrites freed pages.

Not erased:
- the original file (Hollis doesn't own it);
- run logs that retrieved passages (`KnowledgeRetrieved` events, tool results, responses quoting them);
- backups, and file-system copies.

Replacing a source never leaves old chunks retrievable (atomic swap; packing also re-checks versions).

**Audit:** the knowledge change log records actions, keys and actors without content. Runs record which passages (ids, versions, scores) were shown. Logs carry counts, not passage text.

## Alternatives considered

- **Deleting flagged content:** false positives would lose legitimate documentation, and attackers can paraphrase anyway.
- **Model-based grounding judges:** costly, nondeterministic, and themselves injectable. The lexical check is cheap and auditable; a model judge can be layered on by applications.
- **Signing attributions:** citation keys are assigned by Hollis inside the run, not supplied by documents, so signatures add little within one process.

## Trade-offs

- Pattern flags miss paraphrased injections. Framing and authorization remain the real controls.
- The grounding check under-credits correct paraphrases (`weak`) and over-credits negations.
- The run log keeps the passages a run saw. That's needed for audit and replay, and it's the copy deletion can't reach.

## Consequences

- **Evidence:**
  - `tests/security/test_knowledge_security.py`: leakage through each retriever and tool, cache leakage, hidden and invisible smuggling, tool triggering, forged citations, identity and trust, path exposure, forged tool evidence, honest deletion.
  - `tests/knowledge/test_rag_runtime.py`, and the grounding-verifier labels in `tests/evals/rag/`.
- **Not verified:** how real models respond to the framing and citation instructions. The opt-in live RAG evaluation exists and hasn't run.
