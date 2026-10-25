"""Recurring-series detection, decisions, and calendar occurrences."""

import math
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Any, Final, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import Change, applied_batch_for_key, apply, make_preview, snapshot
from omsync.core.months import month_end, shift_months
from omsync.ledger.categories import NOT_IN_REPORTS
from omsync.ledger.models import (
    Account,
    Cadence,
    Category,
    RecurringPaidElsewhere,
    RecurringPrice,
    RecurringRejection,
    RecurringSeries,
    SeriesStatus,
    Transaction,
)
from omsync.reconcile.text import normalize_payee

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


# Schedule math


def next_date(last: date, cadence: Cadence, anchor_day: int | None = None) -> date:
    """The next due date after `last`.

    Weekly and biweekly add 7 or 14 days. Monthly, quarterly, and yearly step 1, 3, or 12 calendar
    months and land on anchor_day, clamped to the month's last day: January 31 with anchor 31 gives
    February 28 (February 29 in a leap year), then March 31. Without an anchor, the day of `last`
    is kept, so the day drifts after a clamped month.
    """
    return _step(last, cadence, anchor_day, 1)


def _step(day: date, cadence: Cadence, anchor_day: int | None, direction: int) -> date:
    if anchor_day is not None and not 1 <= anchor_day <= 31:
        raise ValueError(f"an anchor day is 1 to 31, not {anchor_day}")
    schedule = _SCHEDULES[cadence]
    if schedule.months == 0:
        return day + timedelta(days=direction * round(schedule.period_days))
    return shift_months(day, direction * schedule.months, anchor_day=anchor_day)


def _anchor_day(dates: Iterable[date]) -> int:
    """The median day of the month a charge posts on. Month-based due dates land on it."""
    return int(statistics.median(day.day for day in dates))


def monthly_equivalent_cents(amount_cents: int, cadence: Cadence) -> int:
    """What a bill costs in an average month, in whole cents: a weekly bill is 52/12 of its amount,
    a yearly one 1/12. The magnitude rounds half up, and the sign stays, so a bill stays
    negative."""
    numerator, denominator = _MONTHLY_RATIOS[cadence]
    magnitude = (2 * abs(amount_cents) * numerator + denominator) // (2 * denominator)
    return magnitude if amount_cents >= 0 else -magnitude


def bills_per_month_cents(session: Session, series: Iterable[RecurringSeries]) -> int:
    """The monthly cost of the outflow bills among `series`, as a magnitude, at the amount in effect
    now. Inflows and transfers are left out."""
    prices = _prices_by_series(session)
    total = 0
    for row in series:
        amount = _current_cents(row, prices.get(row.id, []))
        if amount < 0 and not is_transfer(session, row):
            total -= monthly_equivalent_cents(amount, row.cadence)
    return total


# Payee keys


_RAW_STAR_CODE = re.compile(r"\*\s*(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{4,}")  # MKTPL*R715M7ZM1
_NOISE = (
    re.compile(r"\bconf(?:irmation)?\s*(?:#|no\.?|number)?\s*:?\s*\S+"),
    re.compile(r"\b(?:ref|reference|trace|txn|transaction)\s*(?:#|no\.?|id)?\s*:?\s*\S*\d\S*"),
    re.compile(r"\b(?:web\s+)?id:\s*\S+"),
    re.compile(r"\bx{3,}\w*"),  # masked account numbers
    re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"),  # phone numbers
    re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b"),  # dates such as 01/15
    re.compile(r"\*\s*(?=\w*\d)\w{4,}"),  # order and reference codes after a star
    re.compile(r"\b(?=[a-z]*\d)(?=\d*[a-z])[a-z0-9]{6,}\b"),  # mixed letter and digit codes
    re.compile(r"\b\d{6,}\b"),  # long reference numbers
    re.compile(r"\b(?:ppd|ccd|web|tel|ctx)\s*$"),  # trailing ACH codes
)
_EDGE_PUNCT = re.compile(r"^[\s;:,.\-*/]+|[\s;:,.\-*/]+$")
_WS = re.compile(r"\s+")


def payee_key(payee_raw: str | None) -> str:
    """The grouping key for a raw payee: normalized, then stripped of per-transaction noise such as
    reference numbers, order codes, masked account numbers, phone numbers, and dates. It is empty
    when nothing is left."""
    base = normalize_payee(_RAW_STAR_CODE.sub(" ", payee_raw or ""))
    text = base
    for pattern in _NOISE:
        text = pattern.sub(" ", text)
    cleaned = _EDGE_PUNCT.sub("", _WS.sub(" ", text)).strip()
    return cleaned or base


_REGEX_META = re.compile(r"([.^$*+?{}\[\]\\|()])")


def _escape(key: str) -> str:
    """A payee key as a regex that matches only that key: regex metacharacters are escaped, and
    spaces and hyphens are left readable."""
    return _REGEX_META.sub(r"\\\1", key)


# Detection


@dataclass(frozen=True)
class _Txn:
    id: int
    account_id: int
    posted_on: date
    amount: int  # signed cents
    payee: str
    category_id: int | None
    key: str

    @property
    def sign(self) -> int:
        return 1 if self.amount > 0 else -1


def detect(session: Session, *, today: date, lookback_days: int = 800) -> list[SeriesSuggestion]:
    """Suggest recurring series from transactions posted in the lookback window up to `today`.

    Highest confidence first. Writes nothing. A pattern that is already confirmed or dismissed is
    left out. A yearly series needs three charges, so it needs a lookback of more than two years.
    """
    txns = _drop_cross_account_duplicates(
        _load(session, since=today - timedelta(days=lookback_days), until=today)
    )
    groups: dict[tuple[str, int, int], list[_Txn]] = defaultdict(list)
    for txn in txns:
        groups[(txn.key, txn.sign, txn.account_id)].append(txn)
    found: list[SeriesSuggestion] = []
    for group in groups.values():
        if len(group) >= MIN_OCCURRENCES:
            found.extend(_cluster_group(group, today=today))
    decided = _decided_rows(session)
    suggestions = [
        suggestion
        for suggestion in found
        if suggestion.confidence >= MIN_CONFIDENCE
        and not any(_is_same_series(row, suggestion, current) for row, current in decided)
    ]
    return sorted(
        suggestions,
        key=lambda s: (-s.confidence, s.account_id, s.payee_pattern, s.amount_cents),
    )


def review_queue(session: Session, *, today: date) -> list[SeriesSuggestion]:
    """The suggestions to review, most confident first, then by name. The review drawer walks
    them in this order, and the Overview counts them. Writes nothing."""
    return sorted(
        detect(session, today=today),
        key=lambda s: (
            -s.confidence,
            s.name.casefold(),
            s.account_id,
            s.payee_pattern,
            s.amount_cents,
        ),
    )


def _load(session: Session, *, since: date, until: date) -> list[_Txn]:
    rows = session.execute(
        select(
            Transaction.id,
            Transaction.account_id,
            Transaction.posted_on,
            Transaction.amount_cents,
            Transaction.payee,
            Transaction.payee_raw,
            Transaction.category_id,
        )
        .where(
            Transaction.posted_on >= since,
            Transaction.posted_on <= until,
            Transaction.amount_cents != 0,
        )
        .order_by(Transaction.posted_on, Transaction.id)
    ).all()
    txns: list[_Txn] = []
    for row in rows:
        key = payee_key(row.payee_raw)
        if key:
            txns.append(
                _Txn(
                    row.id,
                    row.account_id,
                    row.posted_on,
                    row.amount_cents,
                    row.payee,
                    row.category_id,
                    key,
                )
            )
    return txns


def _drop_cross_account_duplicates(txns: list[_Txn]) -> list[_Txn]:
    """Drop the later of two charges with the same payee and amount in different accounts within
    three days, so one payment seen on both statements counts once."""
    kept: list[_Txn] = []
    recent: dict[tuple[str, int], list[_Txn]] = defaultdict(list)
    for txn in sorted(txns, key=lambda t: (t.posted_on, t.id)):
        bucket = recent[(txn.key, txn.amount)]
        if any(
            other.account_id != txn.account_id
            and abs((txn.posted_on - other.posted_on).days) <= DUPLICATE_WINDOW_DAYS
            for other in bucket
        ):
            continue
        bucket.append(txn)
        kept.append(txn)
    return kept


def _cluster_group(group: list[_Txn], *, today: date) -> list[SeriesSuggestion]:
    """The suggestions for one (payee, sign, account) group, in order of preference.

    1. The whole group, when its amounts are consistent: subscriptions and steady bills.
    2. Fixed-amount bands, when it is not: a subscription inside noisy purchases, or two plans at
       one merchant.
    3. The whole group with varying amounts, only when very regular: card autopay.
    """
    whole = _evaluate(group, strict=True, today=today)
    if whole is not None:
        return [whole]
    bands = [
        result
        for band in _fixed_amount_bands(group)
        if len(band) < len(group)
        and (result := _evaluate(band, strict=True, today=today)) is not None
    ]
    if bands:
        return bands
    variable = _evaluate(group, strict=False, today=today)
    return [variable] if variable is not None else []


def _fixed_amount_bands(txns: list[_Txn]) -> list[list[_Txn]]:
    """Chain charges whose sorted absolute amounts sit within max($0.50, 3%) of each other."""
    bands: list[list[_Txn]] = []
    for txn in sorted(txns, key=lambda t: abs(t.amount)):
        if bands:
            previous = abs(bands[-1][-1].amount)
            if abs(txn.amount) - previous <= max(BAND_FLOOR_CENTS, BAND_RATIO * previous):
                bands[-1].append(txn)
                continue
        bands.append([txn])
    return bands


def _evaluate(txns: list[_Txn], *, strict: bool, today: date) -> SeriesSuggestion | None:
    """Score one candidate cluster. Returns a suggestion, or None when it is not recurring."""
    if len(txns) < MIN_OCCURRENCES:
        return None
    txns = sorted(txns, key=lambda t: (t.posted_on, t.id))
    dates = sorted({txn.posted_on for txn in txns})
    if len(dates) < MIN_OCCURRENCES:
        return None
    gaps = [(later - earlier).days for earlier, later in zip(dates, dates[1:], strict=False)]
    cadence = _classify(dates, gaps)
    if cadence is None:
        return None
    schedule = _SCHEDULES[cadence]
    regularity = _regularity(gaps, schedule)
    if regularity < MIN_REGULARITY:
        return None

    amounts = [abs(txn.amount) for txn in txns]
    mean = statistics.mean(amounts)
    cv = statistics.stdev(amounts) / mean if len(amounts) > 1 else 0.0
    if cv > MAX_CV and (
        strict or len(txns) < VARIABLE_MIN_OCCURRENCES or regularity < VARIABLE_MIN_REGULARITY
    ):
        return None

    typical = round(statistics.median(amounts))
    anchor = _anchor_day(dates)
    last_seen = dates[-1]
    occurrence_score = min(len(dates) / schedule.target_occurrences, 1.0)
    amount_score = max(0.0, 1.0 - cv / MAX_CV)
    confidence = (
        0.30 * occurrence_score
        + 0.25 * amount_score
        + 0.30 * regularity
        + 0.15 * _day_consistency(dates, cadence, anchor)
    )
    if (today - last_seen).days > 2 * schedule.period_days + schedule.tolerance_days:
        confidence *= 0.7  # no charge for about two cycles: the series may have stopped

    name_counts = Counter(txn.payee for txn in txns)
    category_counts = Counter(txn.category_id for txn in txns if txn.category_id is not None)
    sign = txns[0].sign
    return SeriesSuggestion(
        name=min(name_counts, key=lambda name: (-name_counts[name], name.casefold())),
        payee_pattern=_escape(txns[0].key),
        account_id=txns[0].account_id,
        category_id=(
            min(category_counts, key=lambda c: (-category_counts[c], c))
            if category_counts
            else None
        ),
        cadence=cadence,
        amount_cents=sign * typical,
        amount_tolerance_cents=max(
            MIN_TOLERANCE_CENTS, max(abs(amount - typical) for amount in amounts)
        ),
        last_seen=last_seen,
        next_due=next_date(last_seen, cadence, anchor),
        occurrences=tuple(txn.id for txn in txns),
        confidence=round(min(confidence, 1.0), 4),
    )


def _classify(dates: list[date], gaps: list[int]) -> Cadence | None:
    """The cadence whose period is nearest the median gap. A charge paid on the same two days
    each month (semi-monthly) looks biweekly by gap, but has no cadence here, so it is skipped.
    """
    median_gap = statistics.median(gaps)
    best = min(
        _SCHEDULES,
        key=lambda cadence: abs(math.log(median_gap / _SCHEDULES[cadence].period_days)),
    )
    if best is Cadence.biweekly and _two_day_pattern(dates):
        return None
    return best


def _two_day_pattern(dates: list[date]) -> bool:
    """True when the days of the month fall into two tight clusters, such as the 1st and 15th."""
    days = sorted(min(day.day, 30) for day in dates)  # treat the 31st as month end
    if len(days) < 4:
        return False
    split = max(range(1, len(days)), key=lambda i: days[i] - days[i - 1])
    low, high = days[:split], days[split:]
    return (low[-1] - low[0]) <= 5 and (high[-1] - high[0]) <= 5


def _regularity(gaps: list[int], schedule: _Schedule) -> float:
    """The share of gaps that sit on the period, or on a small whole number of periods."""
    if not gaps:
        return 0.0
    regular = 0
    for gap in gaps:
        cycles = max(1, round(gap / schedule.period_days))
        if (
            cycles <= MAX_SKIPPED_CYCLES
            and abs(gap - cycles * schedule.period_days) <= schedule.tolerance_days
        ):
            regular += 1
    return regular / len(gaps)


def _day_consistency(dates: list[date], cadence: Cadence, anchor: int) -> float:
    """The share of charges on the usual weekday (weekly, biweekly) or near the usual day."""
    if cadence in (Cadence.weekly, Cadence.biweekly):
        mode = Counter(day.weekday() for day in dates).most_common(1)[0][0]
        near = sum(1 for day in dates if _cyclic_distance(day.weekday(), mode, 7) <= 1)
        return near / len(dates)
    near = sum(
        1 for day in dates if min(abs(day.day - anchor), 30 - abs(day.day - anchor)) <= BUFFER_DAYS
    )
    return near / len(dates)


def _cyclic_distance(a: int, b: int, period: int) -> int:
    difference = abs(a - b)
    return min(difference, period - difference)


# Decisions and matching


def _band_overlaps(candidate: int, stored: int, tolerance: int) -> bool:
    """Whether a candidate absolute amount overlaps a stored series' band, with the earlier
    service's slack of 15% and a dollar."""
    low = stored - tolerance
    high = stored + tolerance
    return (
        low * _OVERLAP_LOW - _OVERLAP_SLACK_CENTS
        <= candidate
        <= high * _OVERLAP_HIGH + _OVERLAP_SLACK_CENTS
    )


def _is_same_series(row: RecurringSeries, suggestion: SeriesSuggestion, current_cents: int) -> bool:
    """Whether a stored series is this pattern: the same payee, account, and cadence, the same
    sign, and an amount inside the stored series' band, measured from the amount in effect now. A
    price change within the band still matches; a different plan at the same merchant does not."""
    return _same_bill(
        row,
        suggestion.payee_pattern,
        suggestion.account_id,
        suggestion.cadence,
        suggestion.amount_cents,
        current_cents,
    )


def _same_bill(
    row: RecurringSeries,
    payee_pattern: str,
    account_id: int | None,
    cadence: Cadence,
    amount_cents: int,
    current_cents: int,
) -> bool:
    return (
        row.payee_pattern == payee_pattern
        and row.account_id == account_id
        and row.cadence == cadence
        and (current_cents > 0) == (amount_cents > 0)
        and _band_overlaps(abs(amount_cents), abs(current_cents), row.amount_tolerance_cents)
    )


def _decided_rows(session: Session) -> list[tuple[RecurringSeries, int]]:
    """The confirmed, ended, and dismissed series, each with the amount in effect now. An ended
    series is decided, so its pattern is not suggested again."""
    rows = list(
        session.scalars(
            select(RecurringSeries).where(
                RecurringSeries.status.in_(
                    [SeriesStatus.confirmed, SeriesStatus.ended, SeriesStatus.dismissed]
                )
            )
        )
    )
    prices = _prices_by_series(session)
    return [(row, _current_cents(row, prices.get(row.id, []))) for row in rows]


def _rows_with_pattern(session: Session, payee_pattern: str) -> list[RecurringSeries]:
    return list(
        session.scalars(
            select(RecurringSeries).where(RecurringSeries.payee_pattern == payee_pattern)
        )
    )


@dataclass(frozen=True)
class _Ledger:
    """What one read of a series sees: its candidate charges, split by whether they are in the
    amount band, and its decisions. Read once per call, not once per transaction."""

    charges: list[Transaction]  # payee, sign, and account fit, and not rejected
    matched: list[Transaction]  # the charges within the band of the amount in effect
    price_changes: list[Transaction]  # the charges outside it: a new amount, if the user accepts
    prices: list[RecurringPrice]  # oldest first
    paid_elsewhere: set[date]


def _read(session: Session, series: RecurringSeries, *, since: date | None = None) -> _Ledger:
    query = select(Transaction).where(Transaction.amount_cents != 0)
    if series.account_id is not None:
        query = query.where(Transaction.account_id == series.account_id)
    if since is not None:
        query = query.where(Transaction.posted_on >= since)
    query = query.order_by(Transaction.posted_on, Transaction.id)
    pattern = re.compile(series.payee_pattern)
    rejected = set(
        session.scalars(
            select(RecurringRejection.transaction_id).where(
                RecurringRejection.series_id == series.id
            )
        )
    )
    prices = _prices(session, series.id)
    charges: list[Transaction] = []
    matched: list[Transaction] = []
    price_changes: list[Transaction] = []
    for txn in session.scalars(query):
        if txn.id in rejected or not _fits(series, pattern, txn):
            continue
        charges.append(txn)
        deviation = abs(abs(txn.amount_cents) - abs(amount_on(series, txn.posted_on, prices)))
        if deviation <= series.amount_tolerance_cents:
            matched.append(txn)
        else:
            price_changes.append(txn)
    paid_elsewhere = set(
        session.scalars(
            select(RecurringPaidElsewhere.due_on).where(
                RecurringPaidElsewhere.series_id == series.id
            )
        )
    )
    return _Ledger(charges, matched, price_changes, prices, paid_elsewhere)


def _fits(series: RecurringSeries, pattern: re.Pattern[str], txn: Transaction) -> bool:
    """Whether a charge has the series' sign, account, and payee. The amount is checked apart."""
    return (
        (txn.amount_cents > 0) == (series.amount_cents > 0)
        and (series.account_id is None or txn.account_id == series.account_id)
        and pattern.fullmatch(payee_key(txn.payee_raw)) is not None
    )


def _prices(session: Session, series_id: int) -> list[RecurringPrice]:
    return list(
        session.scalars(
            select(RecurringPrice)
            .where(RecurringPrice.series_id == series_id)
            .order_by(RecurringPrice.effective_on)
        )
    )


def _prices_by_series(session: Session) -> dict[int, list[RecurringPrice]]:
    grouped: dict[int, list[RecurringPrice]] = defaultdict(list)
    for price in session.scalars(select(RecurringPrice).order_by(RecurringPrice.effective_on)):
        grouped[price.series_id].append(price)
    return grouped


def _current_cents(series: RecurringSeries, prices: list[RecurringPrice]) -> int:
    return prices[-1].amount_cents if prices else series.amount_cents


def current_amount(session: Session, series: RecurringSeries) -> int:
    """The signed amount in effect now: the latest price row's amount, or the series' own amount
    when it has none. `amount_cents` alone is the amount before any price row."""
    return _current_cents(series, _prices(session, series.id))


def amount_on(series: RecurringSeries, day: date, prices: list[RecurringPrice]) -> int:
    """The signed amount in effect on `day`: the latest price row on or before it, else the series'
    own amount. `prices` are the series' rows, oldest first, as _prices reads them, so a caller
    reads them once for many days."""
    amount = series.amount_cents
    for price in prices:
        if price.effective_on > day:
            break
        amount = price.amount_cents
    return amount


def _grid(series: RecurringSeries, ledger: _Ledger) -> tuple[int, date, date]:
    """The anchor day of the schedule, a due date to step from, and the earliest day it shows.

    With a matched charge, the anchor is the median day of the matched charges. The schedule shows
    from the earlier of the start and the first matched charge, less the window. The start is
    included so that rejecting the first charge leaves its due date to be judged as missed, rather
    than dropping it. With no matched charge, the anchor is the median day of the price changes
    (or the start's day), and the schedule shows from the start.
    """
    cadence = series.cadence
    if ledger.matched:
        anchor = _anchor_day(txn.posted_on for txn in ledger.matched)
        window = timedelta(days=_SCHEDULES[cadence].tolerance_days)
        floor = min(series.starts_on, ledger.matched[0].posted_on) - window
    else:
        dated = ledger.price_changes
        anchor = _anchor_day(txn.posted_on for txn in dated) if dated else series.starts_on.day
        floor = series.starts_on
    return anchor, _phase(series.starts_on, cadence, anchor), floor


def _phase(starts_on: date, cadence: Cadence, anchor: int) -> date:
    """A due date to step the schedule from. A month-based schedule uses the anchor day in the
    start's month, so a first charge posted late does not set the schedule's day."""
    if _SCHEDULES[cadence].months == 0:
        return starts_on
    return shift_months(starts_on, 0, anchor_day=anchor)


def series_transactions(
    session: Session, series: RecurringSeries, *, since: date | None
) -> list[Transaction]:
    """The matched charges of a series, oldest first.

    A charge matches when it is in the series' account (any account when it has none), its
    normalized payee matches the pattern, its sign is the series' sign, its amount is within the
    tolerance of the amount in effect on its posted date, and the user has not rejected it. Only
    charges posted on or after `since` are read; None reads them all.
    """
    return _read(session, series, since=since).matched


def is_transfer(session: Session, series: RecurringSeries) -> bool:
    """Whether the series' category is left out of reports, which is where transfers are kept.
    The ledger has no transfer type of its own."""
    if series.category_id is None:
        return False
    category = session.get(Category, series.category_id)
    return category is not None and category.type in NOT_IN_REPORTS


# Amounts


def fixed_tolerance_cents(amount_cents: int) -> int:
    """The automatic tolerance of a fixed amount: the larger of a dollar and 3% of its size, with
    the 3% rounded half up."""
    return max(MIN_TOLERANCE_CENTS, (3 * abs(amount_cents) + 50) // 100)


def observed_tolerance_cents(session: Session, series: RecurringSeries) -> int:
    """How far the series' charges have strayed from the amount in effect on their dates: the
    largest such deviation, and never less than the fixed tolerance. A Varies amount shows this."""
    ledger = _read(session, series)
    deviations = [
        abs(abs(txn.amount_cents) - abs(amount_on(series, txn.posted_on, ledger.prices)))
        for txn in ledger.charges
    ]
    return max([fixed_tolerance_cents(series.amount_cents), *deviations])


def amount_mode(series: RecurringSeries) -> Literal["fixed", "varies"]:
    """Fixed when the stored tolerance is no wider than the automatic one, else varies."""
    if series.amount_tolerance_cents <= fixed_tolerance_cents(series.amount_cents):
        return "fixed"
    return "varies"


def price_history(session: Session, series: RecurringSeries) -> list[tuple[date | None, int]]:
    """The series' amounts, oldest first: its own amount with no date, then each price row by the
    date it took effect."""
    return [
        (None, series.amount_cents),
        *((price.effective_on, price.amount_cents) for price in _prices(session, series.id)),
    ]


# Schedule reads


def last_seen(session: Session, series: RecurringSeries, *, today: date) -> date | None:
    """The day of the latest matched charge posted on or before `today`, or None when none has."""
    posted = [
        txn.posted_on
        for txn in series_transactions(session, series, since=None)
        if txn.posted_on <= today
    ]
    return max(posted, default=None)


_NEXT_DUE_SEARCH_DAYS: Final = 3 * 366  # long enough to reach the next due date of a yearly series


def next_due(session: Session, series: RecurringSeries, *, today: date) -> date | None:
    """The first due date on or after `today` that is not paid yet, or None when there is none,
    as after an end. Reads only."""
    found = _occurrences(
        session,
        series,
        start=today,
        end=today + timedelta(days=_NEXT_DUE_SEARCH_DAYS),
        today=today,
    )
    return next((item.due_on for item in found if item.status is OccurrenceStatus.upcoming), None)


# Writes. Each one is a batch, and a repeated idempotency key returns the first batch.


def _series_values(
    session: Session, suggestion: SeriesSuggestion, status: SeriesStatus
) -> dict[str, Any]:
    """The row a decision inserts. starts_on is the earliest charge the suggestion was built from,
    not its next due date: the schedule starts where the series was first seen, and a next due date
    is a day still to come."""
    first = session.scalar(
        select(func.min(Transaction.posted_on)).where(Transaction.id.in_(suggestion.occurrences))
    )
    return {
        "name": suggestion.name,
        "payee_pattern": suggestion.payee_pattern,
        "account_id": suggestion.account_id,
        "category_id": suggestion.category_id,
        "cadence": suggestion.cadence,
        "amount_cents": suggestion.amount_cents,
        "amount_tolerance_cents": suggestion.amount_tolerance_cents,
        "starts_on": first,
        "ended_on": None,
        "status": status,
    }


def _decision_changes(
    session: Session, suggestion: SeriesSuggestion, status: SeriesStatus
) -> list[Change]:
    """Insert the decided series. A restored suggestion row for the same pattern is replaced, so no
    stale copy is left behind. Deciding a pattern twice is refused."""
    rows = [
        row
        for row in _rows_with_pattern(session, suggestion.payee_pattern)
        if _is_same_series(row, suggestion, current_amount(session, row))
    ]
    decided = [row for row in rows if row.status is not SeriesStatus.suggested]
    if decided:
        raise ValueError(
            f"{suggestion.name} is already {decided[0].status.value}, so it cannot be decided again"
        )
    superseded = [row for row in rows if row.status is SeriesStatus.suggested]
    changes = [
        Change("recurring_series", str(row.id), snapshot(session, "recurring_series", row.id), None)
        for row in superseded
    ]
    changes.append(
        Change("recurring_series", None, None, _series_values(session, suggestion, status))
    )
    return changes


def confirm(session: Session, suggestion: SeriesSuggestion, *, idempotency_key: str) -> Batch:
    """Save a suggestion as a confirmed series, so it is projected onto the calendar."""
    return _write(
        session,
        "recurring_confirm",
        idempotency_key,
        {"name": suggestion.name, "cadence": suggestion.cadence.value},
        lambda: _decision_changes(session, suggestion, SeriesStatus.confirmed),
    )


def dismiss(session: Session, suggestion: SeriesSuggestion, *, idempotency_key: str) -> Batch:
    """Remember a suggestion as not recurring, so detection never suggests it again. Undo the
    batch, or restore it with restore_dismissed, to bring it back."""
    return _write(
        session,
        "recurring_dismiss",
        idempotency_key,
        {"name": suggestion.name, "cadence": suggestion.cadence.value},
        lambda: _decision_changes(session, suggestion, SeriesStatus.dismissed),
    )


def restore_dismissed(session: Session, series_id: int, *, idempotency_key: str) -> Batch:
    """Delete a dismissed series' row. Removing the dismissal lets detection suggest the pattern
    again. Undo the batch to dismiss it again."""

    def build() -> list[Change]:
        row = _series_or_raise(session, series_id)
        if row.status is not SeriesStatus.dismissed:
            raise ValueError(f"series {series_id} is {row.status.value}, not dismissed")
        before = snapshot(session, "recurring_series", series_id)
        return [Change("recurring_series", str(series_id), before, None)]

    return _write(session, "recurring_restore", idempotency_key, {"series_id": series_id}, build)


def update_series(
    session: Session,
    series_id: int,
    *,
    idempotency_key: str,
    name: str | None = None,
    cadence: Cadence | str | None = None,
    amount_cents: int | None = None,
    amount_tolerance_cents: int | None = None,
    category_id: int | None | _Unchanged = UNCHANGED,
) -> Batch:
    """Edit a series. Fields left at their default keep their value. category_id can be set to
    None, which clears it. amount_cents replaces the amount before any price row."""

    def build() -> list[Change]:
        _series_or_raise(session, series_id)
        changes: dict[str, Any] = {}
        if name is not None:
            if not name.strip():
                raise ValueError("a series needs a name")
            changes["name"] = name.strip()
        if cadence is not None:
            changes["cadence"] = Cadence(cadence)
        if amount_cents is not None:
            _check_int(amount_cents, "a series amount")
            if amount_cents == 0:
                raise ValueError("a series amount cannot be zero")
            changes["amount_cents"] = amount_cents
        if amount_tolerance_cents is not None:
            _check_int(amount_tolerance_cents, "a series tolerance")
            if amount_tolerance_cents < 0:
                raise ValueError("a series tolerance cannot be negative")
            changes["amount_tolerance_cents"] = amount_tolerance_cents
        if not isinstance(category_id, _Unchanged):
            if category_id is not None and session.get(Category, category_id) is None:
                raise ValueError(f"there is no category {category_id}")
            changes["category_id"] = category_id
        before = snapshot(session, "recurring_series", series_id)
        return [Change("recurring_series", str(series_id), before, {**before, **changes})]

    return _write(session, "recurring_update", idempotency_key, {"series_id": series_id}, build)


def edited_amount(series: RecurringSeries, magnitude_cents: int) -> int:
    """The signed amount for an edit that gives a magnitude. A bill stays a bill and a paycheck
    stays income, so editing a series never turns an outflow into an inflow."""
    return magnitude_cents if series.amount_cents > 0 else -magnitude_cents


def delete_series(session: Session, series_id: int, *, idempotency_key: str) -> Batch:
    """Delete a series with its amount history, rejections, and paid-elsewhere marks. The children
    are deleted first in the same batch, so undo restores them. Deleting a dismissed series also
    lets detection suggest it again."""

    def build() -> list[Change]:
        _series_or_raise(session, series_id)
        changes: list[Change] = []
        for model, table in (
            (RecurringPrice, "recurring_price"),
            (RecurringRejection, "recurring_rejection"),
            (RecurringPaidElsewhere, "recurring_paid_elsewhere"),
        ):
            child_ids = session.scalars(
                select(model.id).where(model.series_id == series_id).order_by(model.id)
            ).all()
            for child_id in child_ids:
                before = snapshot(session, table, child_id)
                changes.append(Change(table, str(child_id), before, None))
        before = snapshot(session, "recurring_series", series_id)
        changes.append(Change("recurring_series", str(series_id), before, None))
        return changes

    return _write(
        session,
        "recurring_delete",
        idempotency_key,
        {"name": _summary_name(session, series_id), "series_id": series_id},
        build,
    )


def end_series(session: Session, series_id: int, *, ended_on: date, idempotency_key: str) -> Batch:
    """Stop a confirmed series after `ended_on`, the last day it is due. It has no occurrences
    after that day. A matching charge posted later is a question in Needs attention, not a
    revival. Refused unless the series is confirmed."""

    def build() -> list[Change]:
        row = _series_or_raise(session, series_id)
        if row.status is not SeriesStatus.confirmed:
            raise ValueError(f"{row.name} is {row.status.value}, so it cannot be ended")
        _check_date(ended_on, "the end date")
        before = snapshot(session, "recurring_series", series_id)
        after = {**before, "status": SeriesStatus.ended, "ended_on": ended_on}
        return [Change("recurring_series", str(series_id), before, after)]

    return _write(
        session,
        "recurring_end",
        idempotency_key,
        {"name": _summary_name(session, series_id), "series_id": series_id, "ended_on": ended_on},
        build,
    )


def resume_series(session: Session, series_id: int, *, idempotency_key: str) -> Batch:
    """Make an ended series active again. Refused unless the series has ended."""

    def build() -> list[Change]:
        row = _series_or_raise(session, series_id)
        if row.status is not SeriesStatus.ended:
            raise ValueError(f"{row.name} is {row.status.value}, not ended")
        before = snapshot(session, "recurring_series", series_id)
        after = {**before, "status": SeriesStatus.confirmed, "ended_on": None}
        return [Change("recurring_series", str(series_id), before, after)]

    return _write(
        session,
        "recurring_resume",
        idempotency_key,
        {"name": _summary_name(session, series_id), "series_id": series_id},
        build,
    )


def accept_new_amount(
    session: Session, series_id: int, transaction_id: int, *, idempotency_key: str
) -> Batch:
    """Take a charge's amount as the series' amount from that charge's posted date. Earlier months
    keep the amount they were due at, and the occurrence the charge paid stays paid.

    Refused when the series is not confirmed, when the charge is not a charge of the series, when
    the user rejected it, or when a price is already set from that date: one date holds one amount.
    """

    def build() -> list[Change]:
        row = _series_or_raise(session, series_id)
        if row.status is not SeriesStatus.confirmed:
            raise ValueError(f"{row.name} is {row.status.value}, not confirmed")
        txn = _series_charge_or_raise(session, row, transaction_id)
        if _is_rejected(session, series_id, transaction_id):
            raise ValueError("that charge is marked Not this bill, so it cannot set the amount")
        if any(price.effective_on == txn.posted_on for price in _prices(session, series_id)):
            raise ValueError(f"a new amount is already set from {txn.posted_on.isoformat()}")
        values = {
            "series_id": series_id,
            "effective_on": txn.posted_on,
            "amount_cents": txn.amount_cents,
        }
        return [Change("recurring_price", None, None, values)]

    return _write(
        session,
        "recurring_accept_amount",
        idempotency_key,
        {"name": _summary_name(session, series_id), "series_id": series_id},
        build,
    )


def reject_charge(
    session: Session, series_id: int, transaction_id: int, *, idempotency_key: str
) -> Batch:
    """Mark a charge as not this bill. It never matches the series again, so the occurrence it
    would have paid is judged without it: missed unless another charge pays it or it is marked paid
    elsewhere. Works on ended series too, for a charge posted after the end."""

    def build() -> list[Change]:
        row = _series_or_raise(session, series_id)
        _series_charge_or_raise(session, row, transaction_id)
        if _is_rejected(session, series_id, transaction_id):
            raise ValueError("that charge is already marked Not this bill")
        values = {"series_id": series_id, "transaction_id": transaction_id}
        return [Change("recurring_rejection", None, None, values)]

    return _write(
        session,
        "recurring_reject",
        idempotency_key,
        {"name": _summary_name(session, series_id), "series_id": series_id},
        build,
    )


def mark_paid_elsewhere(
    session: Session, series_id: int, due_on: date, *, idempotency_key: str
) -> Batch:
    """Mark one due date of a series paid from an account omsync does not track. It covers that
    due date only. Refused when `due_on` is not a due date of the series' schedule, or is after the
    series ended."""

    def build() -> list[Change]:
        row = _series_or_raise(session, series_id)
        if row.status not in (SeriesStatus.confirmed, SeriesStatus.ended):
            raise ValueError(f"{row.name} is {row.status.value}, so it has no due dates to mark")
        _check_date(due_on, "the due date")
        if row.ended_on is not None and due_on > row.ended_on:
            raise ValueError(f"{row.name} ended on {row.ended_on.isoformat()}")
        ledger = _read(session, row)
        anchor, phase, _ = _grid(row, ledger)
        if not _due_dates(phase, row.cadence, anchor, low=due_on, high=due_on):
            raise ValueError(f"{due_on.isoformat()} is not a due date of {row.name}")
        if due_on in ledger.paid_elsewhere:
            raise ValueError(f"{due_on.isoformat()} is already marked paid elsewhere")
        values = {"series_id": series_id, "due_on": due_on}
        return [Change("recurring_paid_elsewhere", None, None, values)]

    return _write(
        session,
        "recurring_paid_elsewhere",
        idempotency_key,
        {"name": _summary_name(session, series_id), "series_id": series_id},
        build,
    )


def add_series(
    session: Session,
    *,
    name: str,
    payee_pattern: str,
    account_id: int | None,
    cadence: Cadence | str,
    amount_cents: int,
    starts_on: date,
    category_id: int | None = None,
    amount_tolerance_cents: int | None = None,
    idempotency_key: str,
) -> Batch:
    """Add a bill the user typed in, confirmed at once. Its tolerance defaults to the fixed one for
    its amount. A pattern that already covers this bill is refused, unless the only such row is an
    undecided suggestion, which the new series replaces."""

    def build() -> list[Change]:
        if not name.strip():
            raise ValueError("a series needs a name")
        if not payee_pattern:
            raise ValueError("a series needs a payee pattern")
        try:
            re.compile(payee_pattern)
        except re.error as exc:
            raise ValueError(f"the payee pattern is not valid: {exc}") from None
        kind = Cadence(cadence)
        _check_int(amount_cents, "a series amount")
        if amount_cents == 0:
            raise ValueError("a series amount cannot be zero")
        tolerance = (
            fixed_tolerance_cents(amount_cents)
            if amount_tolerance_cents is None
            else amount_tolerance_cents
        )
        _check_int(tolerance, "a series tolerance")
        if tolerance < 0:
            raise ValueError("a series tolerance cannot be negative")
        _check_date(starts_on, "the start date")
        if account_id is not None and session.get(Account, account_id) is None:
            raise ValueError(f"there is no account {account_id}")
        if category_id is not None and session.get(Category, category_id) is None:
            raise ValueError(f"there is no category {category_id}")
        changes: list[Change] = []
        for row in _rows_with_pattern(session, payee_pattern):
            current = current_amount(session, row)
            if not _same_bill(row, payee_pattern, account_id, kind, amount_cents, current):
                continue
            if row.status is not SeriesStatus.suggested:
                raise ValueError(
                    f"{row.name} already covers this bill, and it is {row.status.value}"
                )
            before = snapshot(session, "recurring_series", row.id)
            changes.append(Change("recurring_series", str(row.id), before, None))
        values = {
            "name": name.strip(),
            "payee_pattern": payee_pattern,
            "account_id": account_id,
            "category_id": category_id,
            "cadence": kind,
            "amount_cents": amount_cents,
            "amount_tolerance_cents": tolerance,
            "starts_on": starts_on,
            "ended_on": None,
            "status": SeriesStatus.confirmed,
        }
        changes.append(Change("recurring_series", None, None, values))
        return changes

    return _write(
        session,
        "recurring_add",
        idempotency_key,
        {"name": name.strip(), "cadence": Cadence(cadence).value},
        build,
    )


def payee_pattern_for(payee_raw: str | None) -> str:
    """The payee pattern detection would give a raw payee, so a bill started from a transaction
    matches the charges a suggestion would. Raises ValueError when the payee leaves no key."""
    key = payee_key(payee_raw)
    if not key:
        raise ValueError("the transaction has no payee to build a pattern from")
    return _escape(key)


def _series_charge_or_raise(
    session: Session, series: RecurringSeries, transaction_id: int
) -> Transaction:
    """The transaction, when it has the series' payee, sign, and account. Rejection and price are
    the caller's concern."""
    txn = session.get(Transaction, transaction_id)
    if txn is None:
        raise ValueError(f"there is no transaction {transaction_id}")
    if (txn.amount_cents > 0) != (series.amount_cents > 0):
        raise ValueError(f"that charge has the other sign from {series.name}")
    if series.account_id is not None and txn.account_id != series.account_id:
        raise ValueError(f"that charge is not in the account of {series.name}")
    if re.compile(series.payee_pattern).fullmatch(payee_key(txn.payee_raw)) is None:
        raise ValueError(f"that charge's payee is not {series.name}'s payee")
    return txn


def _is_rejected(session: Session, series_id: int, transaction_id: int) -> bool:
    return (
        session.scalar(
            select(RecurringRejection.id).where(
                RecurringRejection.series_id == series_id,
                RecurringRejection.transaction_id == transaction_id,
            )
        )
        is not None
    )


def _summary_name(session: Session, series_id: int) -> str:
    """The name a batch summary records. A deleted series keeps its batch, so it may be gone."""
    row = session.get(RecurringSeries, series_id)
    return row.name if row is not None else "series"


# Calendar


@dataclass(frozen=True)
class MonthTotals:
    """The headline totals of one month, all magnitudes. Left to pay is the outflows still
    upcoming, with how many there are. Paid is what the paid outflows actually posted. Income
    expected is every inflow due this month, paid or not."""

    left_to_pay_cents: int
    left_to_pay_count: int
    paid_cents: int
    income_expected_cents: int


def month_totals(occurrences: Iterable[Occurrence]) -> MonthTotals:
    """Totals for the occurrences of one month. A missed outflow counts toward neither left to
    pay nor paid: it is listed as needing attention instead. A transfer counts toward nothing,
    since its money is already counted in the ledger."""
    left = left_count = paid = income = 0
    for occurrence in occurrences:
        if occurrence.is_transfer:
            continue
        if occurrence.expected_cents > 0:
            income += occurrence.expected_cents
        elif occurrence.status is OccurrenceStatus.paid:
            paid += abs(
                occurrence.expected_cents
                if occurrence.actual_cents is None
                else occurrence.actual_cents
            )
        elif occurrence.status is OccurrenceStatus.upcoming:
            left += -occurrence.expected_cents
            left_count += 1
    return MonthTotals(left, left_count, paid, income)


def upcoming(session: Session, *, start: date, end: date, today: date) -> list[Occurrence]:
    """The due dates of confirmed and ended series within [start, end], oldest first.

    Each is paid when a charge within the series' timing window of the due date pays it, or when
    it is marked paid elsewhere, and missed when its window closed with neither. Otherwise it is
    upcoming. An ended series is due only up to its ended_on. Reads only.
    """
    rows = session.scalars(
        select(RecurringSeries)
        .where(RecurringSeries.status.in_([SeriesStatus.confirmed, SeriesStatus.ended]))
        .order_by(RecurringSeries.id)
    ).all()
    found: list[Occurrence] = []
    for row in rows:
        found.extend(_occurrences(session, row, start=start, end=end, today=today))
    return sorted(found, key=lambda o: (o.due_on, o.series_name, o.series_id))


class AttentionKind(StrEnum):
    missed = "missed"  # the window closed with no charge and no paid-elsewhere mark
    new_amount = "new_amount"  # a charge outside the band paid the due date
    ambiguous = "ambiguous"  # more than one charge could have paid the due date
    charged_after_end = "charged_after_end"  # a matching charge posted after the series ended


@dataclass(frozen=True)
class AttentionItem:
    """One item for Needs attention. `day` is the due date, or the posted date for a charge after an
    end. expected_cents is the amount in effect that day. actual_cents and transaction_id name the
    charge the question is about, if there is one, and other_transaction_ids the rest of an
    ambiguous pair."""

    kind: AttentionKind
    series_id: int
    series_name: str
    day: date
    expected_cents: int
    actual_cents: int | None
    transaction_id: int | None
    other_transaction_ids: tuple[int, ...]


def needs_attention(session: Session, *, today: date) -> list[AttentionItem]:
    """What needs the person's attention this calendar month, by day, then name. That is the missed
    occurrences whose window closed, the open questions about this month's due dates, and the
    matching charges posted after a series ended. Reads only."""
    month = today.replace(day=1)
    last = month_end(month)
    items: list[AttentionItem] = []
    for occurrence in upcoming(session, start=month, end=last, today=today):
        if occurrence.status is OccurrenceStatus.missed:
            items.append(
                AttentionItem(
                    AttentionKind.missed,
                    occurrence.series_id,
                    occurrence.series_name,
                    occurrence.due_on,
                    occurrence.expected_cents,
                    None,
                    None,
                    (),
                )
            )
        elif occurrence.flag is not None:
            kind = (
                AttentionKind.new_amount
                if occurrence.flag is OccurrenceFlag.new_amount
                else AttentionKind.ambiguous
            )
            items.append(
                AttentionItem(
                    kind,
                    occurrence.series_id,
                    occurrence.series_name,
                    occurrence.due_on,
                    occurrence.expected_cents,
                    occurrence.actual_cents,
                    occurrence.transaction_id,
                    occurrence.other_transaction_ids,
                )
            )
    items.extend(_charged_after_end(session, month=month, last=last))
    return sorted(items, key=lambda item: (item.day, item.series_name, item.series_id, item.kind))


def _charged_after_end(session: Session, *, month: date, last: date) -> list[AttentionItem]:
    items: list[AttentionItem] = []
    ended = session.scalars(
        select(RecurringSeries).where(RecurringSeries.status == SeriesStatus.ended)
    ).all()
    for series in ended:
        if series.ended_on is None:
            continue
        prices = _prices(session, series.id)
        for txn in series_transactions(session, series, since=month):
            if series.ended_on < txn.posted_on <= last:
                items.append(
                    AttentionItem(
                        AttentionKind.charged_after_end,
                        series.id,
                        series.name,
                        txn.posted_on,
                        amount_on(series, txn.posted_on, prices),
                        txn.amount_cents,
                        txn.id,
                        (),
                    )
                )
    return items


def first_per_series(occurrences: Iterable[Occurrence]) -> list[Occurrence]:
    """The earliest occurrence of each series, in the order given."""
    seen: set[int] = set()
    firsts: list[Occurrence] = []
    for item in occurrences:
        if item.series_id not in seen:
            seen.add(item.series_id)
            firsts.append(item)
    return firsts


def _occurrences(
    session: Session, series: RecurringSeries, *, start: date, end: date, today: date
) -> list[Occurrence]:
    """The occurrences of one series due in [start, end], oldest first.

    A due date is paid by its paid-elsewhere mark, or else by the nearest unused in-band charge in
    its window. Failing both, a lone charge from the same payee outside the band pays it, as a new
    amount for the user to decide (see _price_change). Two or more in-band charges in the window
    make it ambiguous. Otherwise
    it is missed once its window closes, and upcoming until then.
    """
    tolerance = _SCHEDULES[series.cadence].tolerance_days
    window = timedelta(days=tolerance)
    if series.ended_on is not None:
        end = min(end, series.ended_on)
    ledger = _read(session, series)
    anchor, phase, floor = _grid(series, ledger)
    transfer = is_transfer(session, series)
    used: set[int] = set()
    found: list[Occurrence] = []
    for due in _due_dates(phase, series.cadence, anchor, low=max(start, floor), high=end):
        expected = amount_on(series, due, ledger.prices)
        if due in ledger.paid_elsewhere:
            found.append(
                Occurrence(
                    series.id,
                    series.name,
                    due,
                    expected,
                    OccurrenceStatus.paid,
                    paid_elsewhere=True,
                    is_transfer=transfer,
                )
            )
            continue
        matched = _window_candidates(due, ledger.matched, used, tolerance)
        candidates = matched or _price_change(due, expected, ledger.price_changes, used, tolerance)
        if candidates:
            txn = candidates[0]
            used.add(txn.id)
            others = tuple(sorted(item.id for item in candidates[1:]))
            flag: OccurrenceFlag | None = None
            if others:
                flag = OccurrenceFlag.ambiguous
            elif not matched:
                flag = OccurrenceFlag.new_amount
            found.append(
                Occurrence(
                    series.id,
                    series.name,
                    due,
                    expected,
                    OccurrenceStatus.paid,
                    transaction_id=txn.id,
                    posted_on=txn.posted_on,
                    actual_cents=txn.amount_cents,
                    flag=flag,
                    other_transaction_ids=others,
                    is_transfer=transfer,
                )
            )
            continue
        status = OccurrenceStatus.missed if today > due + window else OccurrenceStatus.upcoming
        found.append(
            Occurrence(series.id, series.name, due, expected, status, is_transfer=transfer)
        )
    return found


def _window_candidates(
    due: date, candidates: list[Transaction], used: set[int], tolerance: int
) -> list[Transaction]:
    """The unused candidates posted within the window of `due`, nearest first."""
    return sorted(
        (
            txn
            for txn in candidates
            if txn.id not in used and abs((txn.posted_on - due).days) <= tolerance
        ),
        key=lambda txn: (abs((txn.posted_on - due).days), txn.posted_on, txn.id),
    )


def _price_change(
    due: date, expected: int, candidates: list[Transaction], used: set[int], tolerance: int
) -> list[Transaction]:
    """The one charge outside the band that can stand for a new amount, or none. The payee may
    also sell unrelated things, so a price change needs a single out-of-band charge in the window,
    within half to double the amount in effect. Anything else leaves the due date unpaid."""
    found = _window_candidates(due, candidates, used, tolerance)
    if len(found) != 1:
        return []
    size, base = abs(found[0].amount_cents), abs(expected)
    return found if base <= 2 * size and size <= 2 * base else []


def _due_dates(
    first: date, cadence: Cadence, anchor_day: int, *, low: date, high: date
) -> list[date]:
    """Every due date in [low, high] on the schedule that `first` is on, oldest first."""
    if low > high:
        return []
    day = first
    while day > high:
        day = _step(day, cadence, anchor_day, -1)
    while day < low:
        day = _step(day, cadence, anchor_day, 1)
    if day > high:
        return []
    dues = [day]
    earlier = _step(day, cadence, anchor_day, -1)
    while earlier >= low:
        dues.append(earlier)
        earlier = _step(earlier, cadence, anchor_day, -1)
    later = _step(day, cadence, anchor_day, 1)
    while later <= high:
        dues.append(later)
        later = _step(later, cadence, anchor_day, 1)
    return sorted(dues)


# Helpers


def _write(
    session: Session,
    kind: str,
    idempotency_key: str,
    summary: dict[str, object],
    build: Callable[[], list[Change]],
) -> Batch:
    """Apply one batch. A key that already applied returns its batch and builds nothing."""
    existing = applied_batch_for_key(session, idempotency_key)
    if existing is not None:
        return existing
    preview = make_preview(session, kind, build(), summary)
    return apply(session, preview, idempotency_key=idempotency_key)


def _series_or_raise(session: Session, series_id: int) -> RecurringSeries:
    row = session.get(RecurringSeries, series_id)
    if row is None:
        raise ValueError(f"there is no recurring series {series_id}")
    return row


def _check_int(value: object, what: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} must be an int number of cents")


def _check_date(value: object, what: str) -> None:
    if not isinstance(value, date):
        raise TypeError(f"{what} must be a date")
