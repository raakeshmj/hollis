"""A SIMULATED semantic embedder, for testing fusion mechanics only.

It maps words to hand-written concept groups (synonyms the evaluation author chose while knowing
the evaluation queries) on top of the local hashing embedder. It shows how hybrid retrieval behaves
*when* a dense model captures synonyms and paraphrases, and whether exact identifiers survive
fusion with a strong dense signal. Its scores are NOT evidence of any real embedding model's
quality; real semantic quality is measured only by the opt-in live evaluation.
"""

from hollis.adapters.local_embeddings import HashingEmbedder
from hollis.runtime.text import analyze

CONCEPTS: dict[str, str] = {}
for concept, words in {
    "gpu": "gpu cuda graphics card accelerator",
    "memory": "memory ram oom vram",
    "crash": "crash crashed fail failed failure abort aborted error",
    "taxi": "taxi cab cabs ride-hailing rideshare uber transport transportation",
    "reimburse": "expense reimburse reimbursed refund claim",
    "wait": "wait delay backoff pause",
    "retry": "retry retries attempt attempts",
    "freeze": "freeze moratorium",
    "refreeze": "refreeze refrozen defrosted thawed thaw",
    "ship": "ship shipped transport moved courier couriers",
    "sample": "sample samples specimen specimens blood plasma",
    "hospital": "hospital hospitals site sites",
    "lead": "lead leads led head heads",
}.items():
    for word in words.split():
        CONCEPTS[word] = concept


class SimulatedSemanticEmbedder(HashingEmbedder):
    def __init__(self, *, dimensions: int = 512) -> None:
        super().__init__(dimensions=dimensions, deployment="simulated-semantic")

    def vector(self, text: str) -> tuple[float, ...]:
        concepts = [f"concept_{CONCEPTS[w]}" for w in analyze(text) if w in CONCEPTS]
        return super().vector(text + " " + " ".join(concepts * 3))
