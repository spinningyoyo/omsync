"""The rows, drafts, and constants of category administration."""

import re
from collections.abc import Iterable
from dataclasses import dataclass

from omsync.ledger.categorize import (
    Move,
)
from omsync.ledger.models import (
    RuleCounterparty,
)
from omsync.ledger.payment_methods import (
    PaymentMethod,
)

NOTHING_CHANGED = "Nothing has changed."


IN_USE = "in use: hide it instead"


STALE = "This changed elsewhere. Preview again."


SAVED_FILTER_USE = "saved_filter"


RENTAL_NAME_USE = "rental_name"


COSTCO_FOOD_USE = "costco_food"


# A kind of use is a table name, or one of the kinds above. Each has a singular and a plural.
_USE_NAMES = {
    "transaction": ("transaction", "transactions"),
    "split": ("split line", "split lines"),
    "keyword": ("keyword", "keywords"),
    "category_rule": ("rule", "rules"),
    "budget_line": ("budget line", "budget lines"),
    "recurring_series": ("recurring series", "recurring series"),
    SAVED_FILTER_USE: ("saved filter", "saved filters"),
    RENTAL_NAME_USE: ("rental category name", "rental category names"),
    COSTCO_FOOD_USE: ("Costco food setting", "Costco food settings"),
}


# Tables whose category columns a move rewrites by its own rules, not by repointing every row.
_BY_HAND = frozenset({"category_rule"})


_ICON_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


_WORD = re.compile(r"[^\W\d_]+")  # letters of any script, never digits or underscores


class NothingChanged(ValueError):
    """An update asked for the values that are already stored. Nothing was written."""


@dataclass(frozen=True)
class CategoryRow:
    id: int
    name: str
    type: str  # the CategoryType value
    hidden: bool
    icon: str  # a Lucide icon name; the default when none was chosen
    in_use: bool  # something points at it, so it deletes only by moving its uses


@dataclass(frozen=True)
class CategoryUse:
    """How many rows of one kind point at a category. The kind is a table name, or one of the
    kinds for saved filters and rental category names."""

    kind: str
    count: int

    @property
    def text(self) -> str:
        singular, plural = _USE_NAMES.get(self.kind, (self.kind, self.kind))
        return f"{self.count} {singular if self.count == 1 else plural}"


@dataclass(frozen=True)
class KeywordRow:
    id: int
    list_key: str
    pattern: str
    is_regex: bool
    enabled: bool
    category_id: int
    category_name: str
    category_hidden: bool


@dataclass(frozen=True)
class RuleRow:
    """One rule with the names its conditions show, and the sentence that says what it does.
    Rules are listed in position order: the first that holds sets its category."""

    id: int
    position: int
    enabled: bool
    paid_by: tuple[str, ...]  # PaymentMethod values, in the order PaymentMethod lists them
    below_cents: int | None
    at_least_cents: int | None
    counterparty: str | None  # the RuleCounterparty value
    contact_id: int | None
    contact_name: str | None
    account_id: int | None
    account_name: str | None
    to_category_id: int
    to_category_name: str
    to_category_hidden: bool
    sentence: str


@dataclass(frozen=True)
class RuleDraft:
    """A rule as it would be stored: its conditions and the category it sets. Nothing is stored
    until add_rule or update_rule, and preview_rule checks a draft the same way and writes nothing.
    counterparty is a RuleCounterparty, or its value, and paid_by holds PaymentMethods or their
    values."""

    to_category_id: int
    paid_by: Iterable[PaymentMethod | str] = ()
    below_cents: int | None = None
    at_least_cents: int | None = None
    counterparty: RuleCounterparty | str | None = None
    contact_id: int | None = None
    account_id: int | None = None


@dataclass(frozen=True)
class DraftPreview:
    """The transactions whose category a draft would change, were it saved now. count is their
    number, and changes lists them in ID order."""

    count: int
    changes: tuple[Move, ...]
