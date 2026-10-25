"""Fake, in-memory transactions and the rules the register prototype enforces.

Nothing here touches the database. Money is integer cents, parsed and formatted through
omsync.core.money. Payees and amounts are synthetic.
"""

import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import zip_longest

from omsync.core.money import format_cents, to_cents

SPLIT_LABEL = "Split"
EDITABLE_FIELDS: tuple[str, ...] = ("payee", "category", "memo")
CATEGORIES: tuple[str, ...] = (
    "Groceries",
    "Dining",
    "Gas",
    "Utility",
    "Rent",
    "Travel",
    "Shopping",
    "Entertainment",
    "Healthcare",
    "Insurance",
    "Subscriptions",
    "Transport",
    "Education",
    "Gifts",
    "Income",
)

# Generated transactions fall within the year before this date.
_ANCHOR = date(2026, 10, 11)

# (payee, category, smallest and largest magnitude in cents). Income is positive.
_PAYEES: tuple[tuple[str, str, int, int], ...] = (
    ("Maple Grocery", "Groceries", 2_000, 18_000),
    ("Harbor Market", "Groceries", 1_500, 14_000),
    ("Corner Noodle House", "Dining", 900, 6_500),
    ("Bluebird Coffee", "Dining", 350, 1_400),
    ("Riverside Fuel", "Gas", 2_500, 7_500),
    ("City Power Co", "Utility", 6_000, 22_000),
    ("Water & Sewer Dept", "Utility", 3_000, 9_000),
    ("Oak Street Apartments", "Rent", 150_000, 150_000),
    ("Blue Sky Travel", "Travel", 12_000, 90_000),
    ("Commuter Pass Office", "Transport", 300, 3_500),
    ("Northside Hardware", "Shopping", 1_000, 25_000),
    ("Paper Lantern Books", "Shopping", 800, 6_000),
    ("Lakeview Cinema", "Entertainment", 1_200, 4_000),
    ("Summit Climbing Gym", "Entertainment", 6_000, 12_000),
    ("Family Clinic", "Healthcare", 2_500, 20_000),
    ("Pharmacy Corner", "Healthcare", 500, 8_000),
    ("Shield Insurance", "Insurance", 9_000, 15_000),
    ("StreamBox Plus", "Subscriptions", 1_099, 1_699),
    ("Cloud Notes Pro", "Subscriptions", 500, 2_000),
    ("Night School Online", "Education", 4_000, 30_000),
    ("Birthday Gift Shop", "Gifts", 1_500, 12_000),
    ("Employer Payroll", "Income", 180_000, 260_000),
    ("Interest Credit", "Income", 50, 4_000),
)
_MEMOS: tuple[str, ...] = (
    "",
    "",
    "",
    "",
    "reimbursable",
    "split with partner",
    "tax deductible",
    "receipt in folder",
    "see statement",
)


class RegisterError(ValueError):
    """A rejected edit. The message is shown to the person who made it."""


@dataclass
class SplitLine:
    amount_cents: int
    category: str


@dataclass
class Transaction:
    id: int
    date: date
    payee: str
    category: str
    amount_cents: int
    memo: str
    splits: list[SplitLine] = field(default_factory=list)

    @property
    def is_split(self) -> bool:
        return bool(self.splits)

    def value_of(self, field_name: str) -> str:
        """The text shown for an editable field."""
        values = {"payee": self.payee, "category": self.category, "memo": self.memo}
        return values[field_name]

    def set_text(self, field_name: str, value: str) -> None:
        """Save one editable field. Raises RegisterError when the value is not allowed."""
        text = value.strip()
        if field_name == "payee":
            if not text:
                raise RegisterError("Payee cannot be empty.")
            self.payee = text
        elif field_name == "memo":
            self.memo = text
        elif field_name == "category":
            if self.is_split:
                raise RegisterError("This transaction is split. Change its split lines with s.")
            self.category = canonical_category(text)
        else:
            raise ValueError(f"not an editable field: {field_name}")

    def set_splits(self, lines: list[SplitLine]) -> None:
        """Replace the splits. The lines must add up to the amount exactly."""
        total = sum(line.amount_cents for line in lines)
        if len(lines) < 2 or total != self.amount_cents:
            raise RegisterError("A split needs at least 2 lines that add up to the amount.")
        self.splits = list(lines)
        self.category = SPLIT_LABEL


class RegisterStore:
    """The register's transactions, keyed by id and kept in display order."""

    def __init__(self, transactions: Iterable[Transaction]) -> None:
        self._rows = list(transactions)
        self._by_id = {txn.id: txn for txn in self._rows}
        if len(self._by_id) != len(self._rows):
            raise ValueError("transaction ids must be unique")

    def all(self) -> list[Transaction]:
        return list(self._rows)

    def get(self, txn_id: int) -> Transaction | None:
        return self._by_id.get(txn_id)


def generate_transactions(rng: random.Random, count: int = 500) -> list[Transaction]:
    """Return `count` synthetic transactions, newest first, with ids 1 to count."""
    drafts = []
    for _ in range(count):
        payee, category, low, high = rng.choice(_PAYEES)
        magnitude = rng.randint(low, high)
        amount = magnitude if category == "Income" else -magnitude
        day = _ANCHOR - timedelta(days=rng.randrange(365))
        drafts.append((day, payee, category, amount, rng.choice(_MEMOS)))
    drafts.sort(key=lambda draft: draft[0], reverse=True)
    return [
        Transaction(
            id=number,
            date=day,
            payee=payee,
            category=category,
            amount_cents=amount,
            memo=memo,
        )
        for number, (day, payee, category, amount, memo) in enumerate(drafts, start=1)
    ]


def canonical_category(text: str) -> str:
    """Return the category that matches text ignoring case. Raises RegisterError otherwise."""
    cleaned = text.strip()
    if not cleaned:
        raise RegisterError("Pick a category.")
    if cleaned.casefold() == SPLIT_LABEL.casefold():
        raise RegisterError("Split is set by splitting the transaction (s), not typed in.")
    for name in CATEGORIES:
        if name.casefold() == cleaned.casefold():
            return name
    raise RegisterError(f"{cleaned!r} is not a category. Pick one from the list.")


def amount_left(total_cents: int, amounts: Iterable[str]) -> int:
    """Return the total minus the typed amounts. Blank entries count as zero."""
    left = total_cents
    for text in amounts:
        cleaned = text.strip()
        if cleaned:
            left -= _money(cleaned)
    return left


@dataclass(frozen=True)
class SplitCheck:
    """Outcome of validating split lines. `lines` is None when the split is rejected."""

    lines: list[SplitLine] | None
    line_errors: dict[int, str]
    summary: str


def check_splits(total_cents: int, amounts: Sequence[str], categories: Sequence[str]) -> SplitCheck:
    """Validate submitted split lines against the transaction amount.

    Lines with neither an amount nor a category are ignored. Every other line needs a
    non-zero amount, of either sign, and a known category. There must be at least two such
    lines, and they must add up to the amount exactly.
    """
    line_errors: dict[int, str] = {}
    lines: list[SplitLine] = []
    for index, (amount_text, category_text) in enumerate(
        zip_longest(amounts, categories, fillvalue="")
    ):
        if not amount_text.strip() and not category_text.strip():
            continue
        try:
            cents = _line_amount(amount_text)
            category = canonical_category(category_text)
        except RegisterError as exc:
            line_errors[index] = str(exc)
            continue
        lines.append(SplitLine(cents, category))

    if line_errors:
        return SplitCheck(None, line_errors, "Fix the lines marked in red.")
    if len(lines) < 2:
        return SplitCheck(None, {}, "A split needs at least 2 lines.")
    left = total_cents - sum(line.amount_cents for line in lines)
    if left != 0:
        return SplitCheck(
            None,
            {},
            f"Lines must add up to {format_cents(total_cents)} exactly. "
            f"Amount left: {format_cents(left)}.",
        )
    return SplitCheck(lines, {}, "")


def _line_amount(text: str) -> int:
    cleaned = text.strip()
    if not cleaned:
        raise RegisterError("Enter an amount.")
    cents = _money(cleaned)
    if cents == 0:
        raise RegisterError("An amount cannot be zero.")
    return cents


def _money(text: str) -> int:
    try:
        return to_cents(text)
    except ValueError as exc:
        raise RegisterError(f"{text!r} is not an amount.") from exc
