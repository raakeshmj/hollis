"""The specialist evaluation's tasks: synthetic, deterministic, with known answers.

- **data**: sales rows (date, region, product, amount); the question is Q3 2026 revenue per
  region and which region led. A small table (40 rows) fits in one tool result; a large one
  (3,000 rows) does not, so reading the file shows only part of it.
- **review**: a small Python package with three seeded defects (an off-by-one slice, SQL built
  with an f-string, a timing-unsafe token comparison) and clean decoy files.
- **research**: three policy documents; the question needs three facts, and two documents
  disagree on log retention (the newer one, 90 days, is in force).
- **engineering**: a function with an off-by-one bug and a unit test that catches it.
"""

import random
from dataclasses import dataclass
from pathlib import Path

REGIONS: tuple[str, ...] = ("north", "south", "east", "west")
Q3 = ("2026-07-01", "2026-10-01")


def sales_rows(count: int, seed: int = 7) -> list[tuple[str, str, str, float]]:
    rng = random.Random(seed)  # noqa: S311  # synthetic data, not security
    rows: list[tuple[str, str, str, float]] = []
    for _ in range(count):
        month = rng.randint(4, 12)
        day = rng.randint(1, 28)
        region = rng.choice(REGIONS)
        product = rng.choice(("basic", "pro", "team"))
        amount = round(rng.uniform(20, 900), 2)
        rows.append((f"2026-{month:02d}-{day:02d}", region, product, amount))
    return rows


def sales_csv(rows: list[tuple[str, str, str, float]]) -> str:
    lines = ["date,region,product,amount"] + [f"{d},{r},{p},{a:.2f}" for d, r, p, a in rows]
    return "\n".join(lines) + "\n"


def q3_totals(rows: list[tuple[str, str, str, float]]) -> dict[str, float]:
    totals = dict.fromkeys(REGIONS, 0.0)
    for date, region, _, amount in rows:
        if Q3[0] <= date < Q3[1]:
            totals[region] += amount
    return {region: round(total, 2) for region, total in totals.items()}


DATA_QUESTION = (
    "What was the total revenue per region in Q3 2026 (July to September) in data/sales.csv, "
    "and which region led?"
)

# --- review ------------------------------------------------------------------------------------

REVIEW_FILES = {
    "shop/pagination.py": '''"""Pagination helpers."""


def page(items, number, size=20):
    """Items on page `number` (1-based)."""
    start = (number - 1) * size
    return items[start:start + size - 1]


def pages(count, size=20):
    return (count + size - 1) // size
''',
    "shop/orders.py": '''"""Order lookups."""

import sqlite3


def find_order(db: sqlite3.Connection, order_id: str):
    cursor = db.cursor()
    cursor.execute(f"SELECT * FROM orders WHERE id = {order_id}")
    return cursor.fetchone()


def list_orders(db: sqlite3.Connection, customer: str):
    cursor = db.cursor()
    cursor.execute("SELECT * FROM orders WHERE customer = ?", (customer,))
    return cursor.fetchall()
''',
    "shop/auth.py": '''"""API token checks."""

import os


def expected_token() -> str:
    return os.environ["SHOP_TOKEN"]


def authorized(token: str) -> bool:
    expected = expected_token()
    if token == expected:
        return True
    return False
''',
    "shop/money.py": '''"""Money formatting (no defects)."""


def cents(amount: float) -> int:
    return round(amount * 100)


def format_eur(cents: int) -> str:
    return f"{cents / 100:.2f} EUR"
''',
}


@dataclass(frozen=True)
class Defect:
    file: str
    pattern: str
    """What a reviewer searches for (the simulated reviewer's judgement)."""
    title: str
    severity: str

    def line(self) -> int:
        lines = REVIEW_FILES[self.file].splitlines()
        return next(i for i, text in enumerate(lines, start=1) if self.pattern in text)


DEFECTS = (
    Defect("shop/pagination.py", "size - 1]", "page drops its last item (off by one)", "major"),
    Defect("shop/orders.py", 'execute(f"', "SQL built with an f-string (injection)", "critical"),
    Defect("shop/auth.py", "== expected", "token compared with == (timing attack)", "minor"),
)
REVIEW_TASK = "Review the shop package (shop/) for defects."

# --- research ----------------------------------------------------------------------------------

DOCUMENTS = {
    "retention-2024": (
        "retention-2024.md",
        "# Log retention (2024)\n\n## Retention\n\nApplication logs are retained for 30 days "
        "and then deleted.\n\nThis policy was written in 2024.\n",
    ),
    "retention-2026": (
        "retention-2026.md",
        "# Log retention (2026)\n\n## Retention\n\nApplication logs are retained for 90 days "
        "and then deleted. This 2026 policy replaces the 2024 policy.\n",
    ),
    "access": (
        "access.md",
        "# Production access\n\n## Approval\n\nProduction access is approved by the platform "
        "lead on duty.\n\n## Review\n\nAccess is reviewed every quarter.\n",
    ),
    "incidents": (
        "incidents.md",
        "# Incident response\n\n## Severity 1\n\nA severity-1 incident must be acknowledged "
        "within 15 minutes.\n\n## Severity 2\n\nSeverity-2 incidents are acknowledged within "
        "4 hours.\n",
    ),
}


@dataclass(frozen=True)
class Fact:
    query: str
    value: str
    """What a correct statement contains."""
    document: str


FACTS = (
    Fact("how long are application logs retained", "90 days", "retention-2026"),
    Fact("who approves production access", "platform lead", "access"),
    Fact("severity-1 incident acknowledged within", "15 minutes", "incidents"),
)
RESEARCH_QUESTION = (
    "How long are application logs retained, who approves production access, and how fast must "
    "a severity-1 incident be acknowledged?"
)

# --- engineering -------------------------------------------------------------------------------

STATS = '''"""Statistics helpers."""


def mean(values):
    """Arithmetic mean of a non-empty list."""
    return sum(values) / (len(values) - 1)
'''
STATS_TEST = """import unittest

from calc.stats import mean


class MeanTest(unittest.TestCase):
    def test_mean(self):
        self.assertEqual(mean([2, 4, 6]), 4)


if __name__ == "__main__":
    unittest.main()
"""
BUG = "(len(values) - 1)"
FIX = "len(values)"
WRONG_FIX = "(len(values) + 1)"
ENGINEERING_TASK = "mean() in calc/stats.py returns the wrong value; fix it."


def write_repo(root: Path) -> None:
    """Every task's files, in one workspace."""
    for path, text in REVIEW_FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    reset_engineering(root)


def reset_engineering(root: Path) -> None:
    (root / "calc").mkdir(exist_ok=True)
    (root / "calc" / "__init__.py").write_text("")
    (root / "calc" / "stats.py").write_text(STATS)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "__init__.py").write_text("")
    (root / "tests" / "test_stats.py").write_text(STATS_TEST)
