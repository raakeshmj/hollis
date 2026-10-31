"""Identifier-aware text analysis shared by retrieval components (ADR-0023).

Technical text mixes words with identifiers: error codes, versions, hostnames, function names.
Search needs both behaviours: an identifier must be findable *exactly* (whole token), and also by
its parts, so `CUDA_ERROR_OUT_OF_MEMORY` matches "CUDA out of memory" and `getUserById` matches
"get user by id". `analyze` emits each token whole, then its parts.

    analyze("CUDA_ERROR_OUT_OF_MEMORY on eu-west-1 (v2.3.1)")
    -> ['cuda_error_out_of_memory', 'cuda', 'error', 'out', 'of', 'memory', 'on',
        'eu-west-1', 'eu', 'west', '1', 'v2.3.1', 'v2', '3', '1']
"""

import re
import unicodedata

# A token: word characters, optionally joined by . - / : inside (hostnames, versions, paths).
_TOKEN = re.compile(r"\w+(?:[.\-/:@]\w+)*", re.UNICODE)
_PART_SPLIT = re.compile(r"[^0-9A-Za-zÀ-￿]+|_")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

STOPWORDS = frozenset(
    """a an and are as at be been but by can could did do does for from had has have how i if
    in into is it its me my no not of on or our so than that the their them then there
    these they this to up was we were what when where which who why will with would you
    your""".split()  # noqa: SIM905  # compact word list
)


def is_identifier(token: str) -> bool:
    """Code-like tokens: contain `_ . - / : @`, mix letters and digits, or use inner capitals."""
    if any(c in token for c in "_.-/:@"):
        return True
    has_digit = any(c.isdigit() for c in token)
    has_alpha = any(c.isalpha() for c in token)
    return (has_digit and has_alpha) or bool(_CAMEL.search(token))


def tokens(text: str) -> list[str]:
    """Raw tokens with their original case (identifiers kept whole)."""
    return _TOKEN.findall(unicodedata.normalize("NFKC", text))


def parts(token: str) -> list[str]:
    """The lower-cased parts of an identifier (empty for plain words)."""
    if not is_identifier(token):
        return []
    pieces: list[str] = []
    for chunk in _PART_SPLIT.split(token):
        pieces.extend(p for p in _CAMEL.split(chunk) if p)
    lowered = [p.casefold() for p in pieces]
    return [] if lowered == [token.casefold()] else lowered


def analyze(text: str) -> list[str]:
    """Search terms in order: every token lower-cased, followed by its identifier parts."""
    terms: list[str] = []
    for token in tokens(text):
        terms.append(token.casefold())
        terms.extend(parts(token))
    return terms


def query_terms(text: str, *, limit: int = 48) -> list[str]:
    """Distinct analyzed terms of a query, stopwords removed (kept when they are all there is)."""
    distinct = list(dict.fromkeys(analyze(text)))
    content = [t for t in distinct if t not in STOPWORDS and (len(t) > 1 or t.isdigit())]
    return (content or distinct)[:limit]


def identifiers(text: str) -> list[str]:
    """Identifier tokens in a text, original case, distinct, in order."""
    return list(dict.fromkeys(t for t in tokens(text) if is_identifier(t) and len(t) > 2))
