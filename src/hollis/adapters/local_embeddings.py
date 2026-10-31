"""A local, model-free embedder: hashed character n-grams and identifier-aware words.

`HashingEmbedder` needs no model, network or dependency, so dense retrieval works on any machine,
including air-gapped ones. It captures *surface* similarity: shared words, word fragments,
inflections, typos and identifier parts (`CUDA_ERROR_OUT_OF_MEMORY` shares `cuda`, `out`, `memory`
with "CUDA out of memory"). It does **not** capture meaning: synonyms and paraphrases with no
shared fragments stay far apart. Use a real embedding model (through the gateway) for semantics.

Features: identifier-split words and character 3- to 5-grams of each word, hashed (blake2b) into a
fixed number of signed buckets with sublinear term weighting, then L2-normalized.
"""

import hashlib
import math
from collections import Counter

from hollis.runtime.embedding import EmbeddingRequest, EmbeddingResponse, EmbeddingSpace
from hollis.runtime.model import (
    DeploymentDescriptor,
    EmbeddingCapabilities,
    Locality,
    ModelCapabilities,
    Task,
)
from hollis.runtime.text import analyze

PROTOCOL = "local-hashing"


class HashingEmbedder:
    def __init__(self, *, dimensions: int = 512, deployment: str = "local-hashing") -> None:
        self._descriptor = DeploymentDescriptor(
            deployment=deployment,
            provider="hollis",
            protocol=PROTOCOL,
            model=f"hashing-ngram-3-5-v1-{dimensions}",
            locality=Locality.LOCAL,
            deduplicates_requests=True,
            capabilities=ModelCapabilities(
                tasks=frozenset({Task.EMBEDDING}),
                text=None,
                embedding=EmbeddingCapabilities(
                    dimensions=dimensions, max_batch=256, max_input_chars=20_000, normalized=True
                ),
            ),
        )
        self._space = EmbeddingSpace(
            deployment=deployment, model=self._descriptor.model, dimensions=dimensions
        )
        self.calls = 0

    @property
    def descriptor(self) -> DeploymentDescriptor:
        return self._descriptor

    @property
    def space(self) -> EmbeddingSpace:
        return self._space

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        self.calls += 1
        return EmbeddingResponse(
            vectors=tuple(self.vector(text) for text in request.inputs),
            served_model=self._descriptor.model,
        )

    def vector(self, text: str) -> tuple[float, ...]:
        dimensions = self._space.dimensions
        features: Counter[str] = Counter()
        for word in analyze(text):
            features["w:" + word] += 2
            padded = f"<{word}>"
            for n in (3, 4, 5):
                for i in range(max(len(padded) - n + 1, 1)):
                    features[f"g{n}:{padded[i : i + n]}"] += 1
        values = [0.0] * dimensions
        for feature, count in features.items():
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % dimensions
            sign = 1.0 if digest[4] & 1 else -1.0
            values[bucket] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in values))
        return tuple(v / norm for v in values) if norm else tuple(values)
