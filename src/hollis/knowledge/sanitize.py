"""Sanitization of extracted text and detection of embedded instructions (ADR-0025).

Ingested documents are untrusted. Two classes of problem are handled at ingestion:

- Content a human reader would not see but a model would: zero-width and invisible characters,
  bidirectional overrides, Unicode tag characters, control characters (and, in HTML, hidden
  elements, removed by the HTML extractor). They are removed and *counted*; the counts are kept in
  extraction metadata so suspicious sources can be reviewed.
- Text that looks like instructions to a model ("ignore previous instructions", role markers).
  It is not removed (legitimate documents discuss prompt injection), but chunks are *flagged*;
  automatic context excludes flagged chunks by default, and tools show them with a warning.

Pattern matching is a mitigation, not a guarantee: paraphrased instructions are not detected.
Authorization never depends on document content.
"""

import re
import unicodedata

_INVISIBLE = re.compile(
    r"[\u00ad"  # soft hyphen
    r"\u200b-\u200f"  # zero-width space and joiners, LRM and RLM
    r"\u202a-\u202e"  # bidi embeddings and overrides
    r"\u2060-\u2064"  # word joiner, invisible operators
    r"\u2066-\u2069"  # bidi isolates
    r"\ufeff"  # BOM, zero-width no-break space
    r"\U000e0000-\U000e007f]"  # tag characters
)
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "override",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b.{0,40}"
            r"\b(previous|prior|above|earlier|all|system|your)\b.{0,30}"
            r"\b(instructions?|prompts?|rules|directions)",
            re.I | re.S,
        ),
    ),
    (
        "role-marker",
        re.compile(
            r"(<\|?(im_start|im_end|system|assistant)\|?>|\[/?(INST|SYS)\]"
            r"|^\s*(system|assistant)\s*:)",
            re.I | re.M,
        ),
    ),
    (
        "persona",
        re.compile(
            r"\byou are now\b|\bact as (an?|the) (unrestricted|jailbroken|developer)"
            r"|\bdeveloper mode\b",
            re.I,
        ),
    ),
    (
        "secrecy",
        re.compile(r"\b(do not|don't|never) (tell|inform|reveal to|mention to) the user\b", re.I),
    ),
    (
        "exfiltration",
        re.compile(
            r"\b(send|post|upload|exfiltrate|forward)\b.{0,40}"
            r"\b(credentials?|api keys?|passwords?|secrets?|tokens?)\b",
            re.I | re.S,
        ),
    ),
    ("tool-command", re.compile(r"\b(call|invoke|run|execute) the [`'\"]?\w+[`'\"]? tool\b", re.I)),
)


def sanitize(text: str) -> tuple[str, int]:
    """Text without invisible and control characters (newlines and tabs kept), NFC-normalized,
    with normalized line endings; and the number of characters removed."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned, invisible = _INVISIBLE.subn("", text)
    cleaned, control = _CONTROL.subn("", cleaned)
    return unicodedata.normalize("NFC", cleaned), invisible + control


def instruction_flags(text: str) -> tuple[str, ...]:
    """Names of instruction-like patterns found in `text`."""
    return tuple(name for name, pattern in INJECTION_PATTERNS if pattern.search(text))
