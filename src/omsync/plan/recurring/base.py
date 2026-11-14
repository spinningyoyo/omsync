"""The constants and types of recurring series."""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final

from omsync.plan.models import Cadence

MIN_OCCURRENCES = 3


MIN_CONFIDENCE = 0.5


BUFFER_DAYS = 5  # a monthly charge may post this many days from its median day


MAX_CV = 0.30  # amounts may vary this much (coefficient of variation) and still match


MIN_REGULARITY = 0.6  # share of gaps that must sit on the cadence


MAX_SKIPPED_CYCLES = 3  # a gap of up to three cycles still counts, as skipped payments


VARIABLE_MIN_OCCURRENCES = 6  # regular charges with varying amounts, such as card autopay,


VARIABLE_MIN_REGULARITY = 0.85  # need this many occurrences and very regular timing


DUPLICATE_WINDOW_DAYS = 3


MIN_TOLERANCE_CENTS = 100  # every series tolerates at least a dollar


BAND_FLOOR_CENTS = 50  # fixed-amount bands chain amounts within max($0.50, 3%)


BAND_RATIO = 0.03


# Decisions match a stored band with the earlier service's slack: 15% and a dollar.
_OVERLAP_LOW = 0.85


_OVERLAP_HIGH = 1.15


_OVERLAP_SLACK_CENTS = 100


@dataclass(frozen=True)
class _Schedule:
    period_days: float
    tolerance_days: int  # a charge this close to its due date pays it
    target_occurrences: int  # occurrences that give full history confidence
    months: int = 0  # calendar months per cycle; 0 for the day-based cadences


_SCHEDULES: Final[dict[Cadence, _Schedule]] = {
    Cadence.weekly: _Schedule(7, 2, 12),
    Cadence.biweekly: _Schedule(14, 3, 8),
    Cadence.monthly: _Schedule(30.4, 6, 6, months=1),
    Cadence.quarterly: _Schedule(91.3, 15, 4, months=3),
    Cadence.yearly: _Schedule(365.25, 30, 3, months=12),
}


# A cadence's monthly equivalent is the amount times this ratio: numerator over denominator.
_MONTHLY_RATIOS: Final[dict[Cadence, tuple[int, int]]] = {
    Cadence.weekly: (52, 12),
    Cadence.biweekly: (26, 12),
    Cadence.monthly: (1, 1),
    Cadence.quarterly: (1, 3),
    Cadence.yearly: (1, 12),
}


@dataclass(frozen=True)
class SeriesSuggestion:
    """A pattern detection found, waiting for you to confirm or dismiss it.

    amount_cents is the typical amount, signed: a bill is negative, a paycheck positive.
    amount_tolerance_cents is how far a charge may stray from it and still belong to the series.
    occurrences are the transaction IDs the pattern was built from.
    """

    name: str
    payee_pattern: str
    account_id: int
    category_id: int | None
    cadence: Cadence
    amount_cents: int
    amount_tolerance_cents: int
    last_seen: date
    next_due: date
    occurrences: tuple[int, ...]
    confidence: float


class OccurrenceStatus(StrEnum):
    paid = "paid"  # a matching charge posted within the timing window of the due date
    upcoming = "upcoming"  # not paid yet, and the window has not closed
    missed = "missed"  # the window closed with no matching charge


class OccurrenceFlag(StrEnum):
    new_amount = "new_amount"  # paid by a charge outside the band; the user decides
    ambiguous = "ambiguous"  # two or more charges could have paid it


@dataclass(frozen=True)
class Occurrence:
    """One due date of a confirmed or ended series, with the charge that paid it, if any.

    expected_cents is the amount in effect on the due date. other_transaction_ids are the other
    charges that could have paid it, when it is flagged ambiguous. A transfer's occurrences are
    listed but left out of totals.
    """

    series_id: int
    series_name: str
    due_on: date
    expected_cents: int
    status: OccurrenceStatus
    transaction_id: int | None = None
    posted_on: date | None = None
    actual_cents: int | None = None
    flag: OccurrenceFlag | None = None
    paid_elsewhere: bool = False
    other_transaction_ids: tuple[int, ...] = ()
    is_transfer: bool = False


class _Unchanged:
    def __repr__(self) -> str:
        return "UNCHANGED"


UNCHANGED: Final = _Unchanged()
