"""Labelled answers for measuring the grounding verifier itself (authored, not model output).

Each case: an answer, the evidence it had, and the expected classification of each claim in order,
plus whether it abstains. Covers faithful answers, paraphrases, unsupported additions, fabricated
keys, uncited claims, conflicts and abstention. Known blind spot (also labelled): negation.
"""

from hollis.runtime.grounding import Support

S = Support.SUPPORTED
W = Support.WEAK
U = Support.UNCITED
F = Support.FABRICATED

EVIDENCE = {
    "S1": "The daily meal allowance is 60 EUR. Alcohol is not reimbursed.",
    "S2": "Economy class is required for flights shorter than six hours. Business class needs VP approval.",
    "S3": "The vendor states that object storage is replicated across three zones.",
    "S4": "Our object storage is replicated across two zones only.",
}

CASES: list[tuple[str, tuple[Support, ...], bool]] = [
    ("The daily meal allowance is 60 EUR [S1].", (S,), False),
    ("Flights under six hours must be economy class [S2].", (S,), False),
    (
        "Business class requires VP approval [S2]. The meal allowance is 60 EUR per day [S1].",
        (S, S),
        False,
    ),
    (
        "Meals are covered up to 60 EUR [S1], and business class needs approval from a VP [S2].",
        (S,),
        False,
    ),
    (
        "Business class is allowed for any flight longer than two hours without approval [S2].",
        (W,),
        False,
    ),
    ("Hotel stays are capped at 150 EUR per night [S1].", (W,), False),
    ("The meal allowance is 60 EUR [S7].", (F,), False),
    (
        "Business class needs VP approval [S2]. Taxis are reimbursed up to 80 EUR [S9].",
        (S, F),
        False,
    ),
    ("The daily meal allowance is 60 EUR according to the policy.", (U,), False),
    (
        "Our storage is replicated across two zones [S4], although the vendor claims three [S3].",
        (S,),
        False,
    ),
    (
        "Object storage is replicated across two zones [S4]. The vendor documentation says three zones [S3].",
        (S, S),
        False,
    ),
    ("The sources do not say what the hotel limit is.", (), True),
    ("I don't know; the passages do not mention parental leave.", (), True),
    ("There is not enough information in the documents to answer that.", (), True),
    (
        "Alcohol is reimbursed with meals [S1].",
        (S,),
        False,
    ),  # negation blind spot: labelled as the verifier sees it
    ("Security reviews happen quarterly across all teams [S2].", (W,), False),
]
