# ADR-0022: Embeddings as a model task in the gateway

- Status: Accepted (M5)
- Date: 2026-11-01

## Context

Dense retrieval needs embedding models, and so would future memory scorers. The gateway (ADR-0011) served text generation only. Deployments include:
- local servers (Ollama, llama.cpp, LM Studio, Infinity, Hugging Face Text Embeddings Inference);
- self-hosted vLLM;
- hosted APIs (OpenAI and compatible).

Most of them speak the OpenAI `/v1/embeddings` wire format. Vectors from different models, dimensions or model revisions aren't comparable. No embedding model or server was available while this was built.

## Decision

**A separate task.**
- `Task.EMBEDDING`, with `EmbeddingCapabilities`: dimensions, maximum batch, input character limit, normalization.
- An `EmbeddingProvider` port (`hollis.runtime.embedding`) with `EmbeddingRequest` (inputs, plus a query or document input type for models that embed them differently) and `EmbeddingResponse` (vectors, reported tokens, served model).
- It isn't a text-generation request with a different response shape.

**Embedding spaces.**
- `EmbeddingSpace` = deployment, model, dimensions and an operator-declared version. Its key tags every stored vector.
- A collection belongs to exactly one space. Ingestion refuses an embedder of another space.
- Dense search compares only vectors of the requested space, and skips collections of other spaces.
- Changing models is an explicit, atomic `migrate_embeddings`: re-embed everything, then switch, so queries keep using the old space until the switch.

**Adapters.**
- `openai-embeddings` (raw httpx2 over the M2 HTTP layer): explicit endpoint, auth by reference, TLS and proxy configuration.
  - Optional query and document prefixes; optional `dimensions`.
  - Every response is validated for count, order and dimensions; a mismatch is an invalid response.
  - Failures are classified like text generation (ADR-0007).
- `HashingEmbedder` (local, model-free): hashed character 3- to 5-grams and identifier-aware words. It captures surface similarity (identifier parts, inflections, typos), **not meaning**, so dense retrieval works anywhere, including air-gapped hosts.

**Gateway.**
- Catalog `openai-embeddings` deployments must declare `capabilities.embedding.dimensions`.
- `ModelGateway.embedder(id)` applies the same routing policy and credential handling as `resolve`. Text and embedding resolution refuse each other's deployments (`WrongTask`).
- `embed_all` batches, truncates over-long inputs, normalizes, and checks dimensions.

## Alternatives considered

- **Provider SDKs:** they would duplicate the M2 HTTP layer's controls (no environment routing, explicit TLS).
- **Shipping a local neural embedding model** (sentence-transformers or ONNX): a heavy dependency (torch or onnxruntime plus model files), contrary to keeping the core light. Local models are served through Ollama, TEI or llama.cpp instead.
- **sqlite-vec or numpy for vector math:** see ADR-0024. Not needed at the measured scale.
- **Mixing spaces with a projection:** silently degrades results. Explicit migration is safer.

## Trade-offs

- The hashing embedder can't relate synonyms (shown by a test). Semantic quality needs a real model through the gateway.
- Model-specific prompt conventions (prefixes) are configuration the operator must know.
- Embedding calls aren't recorded as run events. They're stateless and safe to retry, and their inputs are indexed content or queries already in the log.

## Consequences

- **Evidence:** `tests/adapters/test_embeddings.py`: wire shape, prefixes, validation, failure classification, batching, gateway resolution, routing policy, and credential redaction.
- `tests/knowledge/test_lifecycle.py`: spaces never mixed; migration is atomic.
- **Not verified:** no live embedding deployment has been tested. Any server's compatibility is *documented*, not *verified* ([matrix](../architecture/model-compatibility.md)).
