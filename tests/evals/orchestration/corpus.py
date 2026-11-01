"""Synthetic corpora and questions for the orchestration evaluation.

Every fact is a single sentence that a search for its question finds; filler sections share the
domain vocabulary so results are long and similar, as in real document sets. Nothing here is
real data, and the questions are not a benchmark: they exist to exercise coordination.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Question:
    text: str
    fact: str
    """A phrase the correct answer contains."""
    collection: str
    wrong: str | None = None
    """A phrase a wrong (stale or conflicting) answer contains."""


FILLER = {
    "ops": [
        "Release notes are drafted by the author of the change and reviewed in the weekly sync.",
        "Incident reviews are blameless and are published on the operations wiki within a week.",
        "Staging environments are refreshed from anonymized production snapshots every night.",
        "Runbooks live next to the service code and are checked by the release tooling.",
        "Paging thresholds are revisited every quarter by the reliability working group.",
        "Feature flags are cleaned up within two sprints after a full rollout.",
    ],
    "finance": [
        "Budgets are planned in the autumn and confirmed by the leadership team in December.",
        "Purchase orders above the team limit need a second approver from finance.",
        "Currency conversion for reports uses the rate published on the first day of the month.",
        "Cost centres are reviewed every quarter and merged when teams reorganize.",
        "Software subscriptions are renewed centrally by the procurement desk.",
        "Reimbursements are paid with the next payroll run after approval.",
    ],
    "legal": [
        "Standard terms are reviewed every year by the commercial legal team.",
        "Non-disclosure agreements use the template in the legal portal without edits.",
        "Trademark questions go to the brand counsel through the legal intake form.",
        "Export control screening runs before any new country is added to the sales list.",
        "Open source licences are approved by the open source review board.",
        "Litigation holds override every retention schedule until they are released.",
    ],
    "product": [
        "Roadmap themes are set twice a year from customer research and usage data.",
        "Beta features are labelled in the interface and can change without notice.",
        "Pricing pages are updated by product marketing after each plan change.",
        "Deprecations are announced at least six months before an endpoint is removed.",
        "Usage analytics are aggregated and never include message content.",
        "Accessibility reviews are part of the definition of done for every feature.",
    ],
}

BREADTH = [
    Question("How do we deploy a release?", "make ship", "ops"),
    Question("Who approves production hotfixes?", "duty manager", "ops"),
    Question("What is the travel budget per team?", "41000 euros", "finance"),
    Question("When are expense reports due?", "fifth working day", "finance"),
    Question("How long are customer contracts retained?", "seven years", "legal"),
    Question("Who signs data processing agreements?", "general counsel", "legal"),
    Question("Which plan includes single sign-on?", "Enterprise plan", "product"),
    Question("What is the public API rate limit?", "600 requests per minute", "product"),
]
FACTS = {
    "make ship": "Releases are deployed with make ship from the release branch.",
    "duty manager": "Production hotfixes are approved by the duty manager on call.",
    "41000 euros": "The travel budget is 41000 euros per team for 2026.",
    "fifth working day": "Expense reports are due on the fifth working day of each month.",
    "seven years": "Customer contracts are retained for seven years after termination.",
    "general counsel": "Data processing agreements are signed by the general counsel.",
    "Enterprise plan": "Single sign-on is included in the Enterprise plan only.",
    "600 requests per minute": "The public API rate limit is 600 requests per minute.",
}
DESCRIPTIONS = {
    "ops": "answers questions about operations: deploying releases, hotfixes, incidents",
    "finance": "answers questions about finance: budgets, travel, expense reports",
    "legal": "answers questions about legal matters: contracts, retention, agreements",
    "product": "answers questions about the product: plans, pricing, single sign-on, API",
}

# Dependent: the second question can only be searched well once the first is answered.
OWNERSHIP = [
    "The billing service is owned by the ledger team.",
    "The search service is owned by the atlas team.",
    "The notifications service is owned by the beacon team.",
    "The atlas team on-call rotation is led by Marta Silva.",
    "The beacon team on-call rotation is led by Tomas Weber.",
    "The ledger team on-call rotation is led by Priya Raman.",
]
DEPENDENT = [
    Question("Which team owns the billing service?", "ledger team", "ownership"),
    Question("Who leads the on-call rotation of that team?", "Priya Raman", "ownership"),
]

# Conflicting: two sources answer the same question differently; the newer one is right.
REFUND_OLD = "Under the 2024 handbook, customers can request a refund within 30 days of purchase."
REFUND_NEW = "Under the 2026 policy, customers can request a refund within 14 days of purchase."
CONFLICT = Question(
    "How many days do customers have to request a refund?", "14 days", "policy", wrong="30 days"
)


def document(collection: str) -> str:
    """A markdown document: facts of the collection, interleaved with filler sections."""
    if collection == "ownership":
        return "# Service ownership\n\n" + "\n\n".join(
            f"## Note {i}\n\n{line}" for i, line in enumerate(OWNERSHIP, start=1)
        )
    facts = [FACTS[q.fact] for q in BREADTH if q.collection == collection]
    lines = list(FILLER[collection])
    for offset, fact in enumerate(facts):
        lines.insert(2 + offset * 3, fact)
    sections = [f"## Section {i}\n\n{line}" for i, line in enumerate(lines, start=1)]
    return f"# {collection.title()} handbook\n\n" + "\n\n".join(sections)
