"""Text normalization shared by memory stores: dedup hashing, terms, and lexical scoring."""

import hashlib
import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence

_WORD = re.compile(r"\w+", re.UNICODE)

STOPWORDS = frozenset(
    """a an and are as at be been but by can could did do does for from had has have how i if
    in into is it its me my no not of on or our so than that the their them then there
    these they this to up was we were what when where which who why will with would you
    your""".split()  # noqa: SIM905  # compact word list
)


def normalize(text: str) -> str:
    """Case-, width- and whitespace-insensitive form used for exact-duplicate detection."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def normalize_subject(subject: str | None) -> str | None:
    if subject is None:
        return None
    value = normalize(subject)
    return value or None


def terms(text: str, *, limit: int = 32) -> list[str]:
    """Distinct, lower-cased content words in order of first appearance (stopwords removed)."""
    seen: dict[str, None] = {}
    for word in _WORD.findall(unicodedata.normalize("NFKC", text).casefold()):
        if len(word) >= 2 and word not in STOPWORDS:
            seen.setdefault(word, None)
        if len(seen) >= limit:
            break
    return list(seen)


def stem(word: str) -> str:
    """A light suffix stripper for the in-memory index (SQLite uses FTS5's Porter stemmer)."""
    for suffix in ("ingly", "edly", "ing", "ied", "ies", "ed", "es", "ly", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)] + ("y" if suffix in ("ied", "ies") else "")
    return word


def bm25_scores(
    query: Sequence[str], documents: Sequence[Sequence[str]], *, k1: float = 1.2, b: float = 0.75
) -> list[float]:
    """Okapi BM25 for already-tokenized documents (the same formula FTS5 implements)."""
    if not documents:
        return []
    count = len(documents)
    average = sum(len(d) for d in documents) / count or 1.0
    frequencies = [Counter(d) for d in documents]
    df = Counter(term for f in frequencies for term in f)
    scores: list[float] = []
    for doc, freq in zip(documents, frequencies, strict=True):
        score = 0.0
        for term in set(query):
            n = df.get(term, 0)
            if not n or term not in freq:
                continue
            idf = math.log((count - n + 0.5) / (n + 0.5) + 1.0)
            tf = freq[term]
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(doc) / average))
        scores.append(score)
    return scores
