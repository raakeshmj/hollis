"""Deterministic answer grounding checks (ADR-0025).

Given an answer and the evidence the run actually had, classify each claim (sentence):
- `supported`: cites evidence that exists and shares most of the claim's content words;
- `weak`: cites existing evidence, but the cited text shares few of the claim's content words;
- `uncited`: makes a claim without citing anything;
- `fabricated`: cites a key or handle the run never had.
It also detects abstention ("the sources do not say").

This is a lexical measurement, not proof: a supported claim can still be wrong (the evidence may
be wrong or misread), and a correct paraphrase can score as weak. It never trusts the model's
claim that a citation supports it, and it never invents evidence.

Evidence handles:
- `S1`, `S2`, ...: passages retrieved at run start (`KnowledgeRetrieved`);
- `C-xxxxxxxx`: passages returned by knowledge tools during the run, each rendered as a block
  starting with its handle. Handle-like text inside documents is neutralized when rendered, so a
  document cannot forge evidence.
"""

import re
from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from hollis.runtime.text import query_terms

CITATION = re.compile(r"\[((?:S\d{1,3}|C-[0-9a-f]{8})(?:\s*[,;]\s*(?:S\d{1,3}|C-[0-9a-f]{8}))*)\]")
HANDLE_LIKE = re.compile(r"\[(S\d{1,3}|C-[0-9a-f]{8})\]")
TOOL_EVIDENCE = re.compile(
    r"^\[(C-[0-9a-f]{8})\][^\n]*\n(.*?)(?=^\[C-[0-9a-f]{8}\]|\Z)", re.M | re.S
)
ABSTAIN = re.compile(
    r"\b(i (do not|don't) know|not enough (information|evidence)|"
    r"insufficient (information|evidence)|"
    r"(cannot|can't|could not|couldn't) (find|determine|confirm)|no (information|evidence|mention)|"
    r"(sources|passages|documents|notes) (do not|don't) (say|mention|contain|cover|specify))",
    re.I,
)
SUPPORTED_OVERLAP = 0.5


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Support(StrEnum):
    SUPPORTED = "supported"
    WEAK = "weak"
    UNCITED = "uncited"
    FABRICATED = "fabricated"


class Claim(_Frozen):
    text: str
    citations: tuple[str, ...] = ()
    support: Support
    overlap: float = 0.0


class GroundingReport(_Frozen):
    evidence: int = Field(ge=0)
    """Evidence items available to the answer."""
    claims: tuple[Claim, ...] = ()
    cited: tuple[str, ...] = ()
    """Valid evidence keys the answer cites."""
    fabricated: tuple[str, ...] = ()
    """Keys the answer cites that the run never had."""
    abstained: bool = False

    def count(self, support: Support) -> int:
        return sum(1 for c in self.claims if c.support is support)

    @property
    def citation_precision(self) -> float | None:
        cited = [c for c in self.claims if c.citations]
        if not cited:
            return None
        return sum(1 for c in cited if c.support is Support.SUPPORTED) / len(cited)


def neutralize(text: str) -> str:
    """Make handle-like text in evidence inert, so documents cannot forge citations."""
    return HANDLE_LIKE.sub(lambda m: f"({m.group(1)})", text)


def tool_evidence(text: str) -> dict[str, str]:
    """Evidence blocks (`[C-xxxxxxxx] header` + body) in a knowledge tool result."""
    return {m.group(1): m.group(2).strip() for m in TOOL_EVIDENCE.finditer(text)}


_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_MARKUP = re.compile(r"^[\s>*#\-+\d.)]+|[*_`]+")


def check(answer: str, evidence: Mapping[str, str], *, max_claims: int = 60) -> GroundingReport:
    claims: list[Claim] = []
    cited: set[str] = set()
    fabricated: set[str] = set()
    abstained = bool(ABSTAIN.search(answer))
    for raw in _SENTENCE.split(answer):
        sentence = _MARKUP.sub("", raw).strip()
        keys = [k.strip() for group in CITATION.findall(sentence) for k in re.split(r"[,;]", group)]
        content = [
            t for t in query_terms(CITATION.sub(" ", sentence)) if not re.fullmatch(r"s\d+|c", t)
        ]
        if len(content) < 3 or ABSTAIN.search(sentence):
            continue  # too short to be a claim, or an abstention
        if not keys:
            claims.append(Claim(text=sentence[:300], support=Support.UNCITED))
            continue
        unknown = [k for k in keys if k not in evidence]
        if unknown:
            fabricated.update(unknown)
            claims.append(
                Claim(text=sentence[:300], citations=tuple(keys), support=Support.FABRICATED)
            )
            continue
        cited.update(keys)
        available = set(query_terms(" ".join(evidence[k] for k in keys), limit=100_000))
        words = set(content)
        overlap = len(words & available) / len(words)
        support = Support.SUPPORTED if overlap >= SUPPORTED_OVERLAP else Support.WEAK
        claims.append(
            Claim(
                text=sentence[:300],
                citations=tuple(keys),
                support=support,
                overlap=round(overlap, 3),
            )
        )
        if len(claims) >= max_claims:
            break
    return GroundingReport(
        evidence=len(evidence),
        claims=tuple(claims),
        cited=tuple(sorted(cited)),
        fabricated=tuple(sorted(fabricated)),
        abstained=abstained,
    )
