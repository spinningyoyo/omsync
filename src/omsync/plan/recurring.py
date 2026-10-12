"""Recurring-series detection, decisions, and calendar occurrences."""

import math
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import Change, applied_batch_for_key, apply, make_preview, snapshot
from omsync.core.months import shift_months
from omsync.ledger.models import Cadence, Category, RecurringSeries, SeriesStatus, Transaction
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


@dataclass(frozen=True)
class Occurrence:
    """One due date of a confirmed series, with the charge that paid it, if any."""

    series_id: int
    series_name: str
    due_on: date
    expected_cents: int
    status: OccurrenceStatus
    transaction_id: int | None = None
    posted_on: date | None = None
    actual_cents: int | None = None


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
        and not any(_is_same_series(row, suggestion) for row in decided)
    ]
    return sorted(
        suggestions,
        key=lambda s: (-s.confidence, s.account_id, s.payee_pattern, s.amount_cents),
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


def _is_same_series(row: RecurringSeries, suggestion: SeriesSuggestion) -> bool:
    """Whether a stored series is this pattern: the same payee, account, and cadence, the same
    sign, and an amount inside the stored series' band. A price change within the band still
    matches; a different plan at the same merchant does not."""
    return (
        row.payee_pattern == suggestion.payee_pattern
        and row.account_id == suggestion.account_id
        and row.cadence == suggestion.cadence
        and (row.amount_cents > 0) == (suggestion.amount_cents > 0)
        and _band_overlaps(
            abs(suggestion.amount_cents),
            abs(row.amount_cents),
            row.amount_tolerance_cents,
        )
    )


def _decided_rows(session: Session) -> list[RecurringSeries]:
    return list(
        session.scalars(
            select(RecurringSeries).where(
                RecurringSeries.status.in_([SeriesStatus.confirmed, SeriesStatus.dismissed])
            )
        )
    )


def _rows_with_pattern(session: Session, payee_pattern: str) -> list[RecurringSeries]:
    return list(
        session.scalars(
            select(RecurringSeries).where(RecurringSeries.payee_pattern == payee_pattern)
        )
    )


def series_transactions(
    session: Session, series: RecurringSeries, *, since: date | None
) -> list[Transaction]:
    """The transactions that belong to a series, oldest first.

    A transaction matches when it is in the series' account (any account when it has none), its
    normalized payee matches the pattern, its sign is the series' sign, and its amount is within the
    tolerance. Only transactions posted on or after `since` are read; None reads them all.
    """
    query = select(Transaction).where(Transaction.amount_cents != 0)
    if series.account_id is not None:
        query = query.where(Transaction.account_id == series.account_id)
    if since is not None:
        query = query.where(Transaction.posted_on >= since)
    query = query.order_by(Transaction.posted_on, Transaction.id)
    pattern = re.compile(series.payee_pattern)
    target = abs(series.amount_cents)
    matched: list[Transaction] = []
    for txn in session.scalars(query):
        if (txn.amount_cents > 0) != (series.amount_cents > 0):
            continue
        if abs(abs(txn.amount_cents) - target) > series.amount_tolerance_cents:
            continue
        if pattern.fullmatch(payee_key(txn.payee_raw)) is not None:
            matched.append(txn)
    return matched


def _series_values(suggestion: SeriesSuggestion, status: SeriesStatus) -> dict[str, Any]:
    return {
        "name": suggestion.name,
        "payee_pattern": suggestion.payee_pattern,
        "account_id": suggestion.account_id,
        "category_id": suggestion.category_id,
        "cadence": suggestion.cadence,
        "amount_cents": suggestion.amount_cents,
        "amount_tolerance_cents": suggestion.amount_tolerance_cents,
        "next_due": suggestion.next_due,
        "last_seen": suggestion.last_seen,
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
        if _is_same_series(row, suggestion)
    ]
    decided = [
        row for row in rows if row.status in (SeriesStatus.confirmed, SeriesStatus.dismissed)
    ]
    if decided:
        raise ValueError(
            f"{suggestion.name} is already {decided[0].status.value}, so it cannot be decided again"
        )
    superseded = [row for row in rows if row.status is SeriesStatus.suggested]
    changes = [
        Change("recurring_series", str(row.id), snapshot(session, "recurring_series", row.id), None)
        for row in superseded
    ]
    changes.append(Change("recurring_series", None, None, _series_values(suggestion, status)))
    return changes


# Writes. Each one is a batch, and a repeated idempotency key returns the first batch.


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
    """Set a dismissed series back to suggested, so detection can suggest it again."""

    def build() -> list[Change]:
        row = _series_or_raise(session, series_id)
        if row.status is not SeriesStatus.dismissed:
            raise ValueError(f"series {series_id} is {row.status.value}, not dismissed")
        before = snapshot(session, "recurring_series", series_id)
        after = {**before, "status": SeriesStatus.suggested}
        return [Change("recurring_series", str(series_id), before, after)]

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
    next_due: date | None | _Unchanged = UNCHANGED,
) -> Batch:
    """Edit a series. Fields left at their default keep their value. category_id and next_due can
    be set to None, which clears them."""

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
        if not isinstance(next_due, _Unchanged):
            changes["next_due"] = next_due
        before = snapshot(session, "recurring_series", series_id)
        return [Change("recurring_series", str(series_id), before, {**before, **changes})]

    return _write(session, "recurring_update", idempotency_key, {"series_id": series_id}, build)


def edited_amount(series: RecurringSeries, magnitude_cents: int) -> int:
    """The signed amount for an edit that gives a magnitude. A bill stays a bill and a paycheck
    stays income, so editing a series never turns an outflow into an inflow."""
    return magnitude_cents if series.amount_cents > 0 else -magnitude_cents


def delete_series(session: Session, series_id: int, *, idempotency_key: str) -> Batch:
    """Delete a series. Deleting a dismissed series also lets detection suggest it again."""

    def build() -> list[Change]:
        _series_or_raise(session, series_id)
        before = snapshot(session, "recurring_series", series_id)
        return [Change("recurring_series", str(series_id), before, None)]

    return _write(session, "recurring_delete", idempotency_key, {"series_id": series_id}, build)


def refresh(session: Session, series_id: int, *, today: date) -> Batch | None:
    """Recompute last_seen and next_due from the series' matched transactions posted up to `today`.

    Returns None and writes nothing when the values are already right, or when no transaction
    matches. Otherwise it is one batch. Its key comes from the data revision it was computed at,
    so it cannot collide with an earlier refresh that was undone.
    """
    series = _series_or_raise(session, series_id)
    dates = [
        txn.posted_on
        for txn in series_transactions(session, series, since=None)
        if txn.posted_on <= today
    ]
    if not dates:
        return None
    last_seen = max(dates)
    next_due = next_date(last_seen, series.cadence, _anchor_day(dates))
    if series.last_seen == last_seen and series.next_due == next_due:
        return None
    before = snapshot(session, "recurring_series", series_id)
    change = Change(
        "recurring_series",
        str(series_id),
        before,
        {**before, "last_seen": last_seen, "next_due": next_due},
    )
    preview = make_preview(session, "recurring_refresh", [change], {"series_id": series_id})
    return apply(
        session,
        preview,
        idempotency_key=f"recurring-refresh:{series_id}:{preview.base_revision}",
    )


# Calendar


@dataclass(frozen=True)
class MonthSummary:
    outflows_cents: int  # expected outflows: every outflow due this month, paid or not
    inflows_cents: int  # expected inflows: every inflow due this month, paid or not
    paid_cents: int  # the amounts that paid occurrences actually posted, by magnitude
    missed_cents: int
    missed_count: int
    due_cents: int  # still due: upcoming occurrences whose window is open
    due_count: int


def summarize(occurrences: Iterable[Occurrence]) -> MonthSummary:
    """Totals for the occurrences of a month. A bill is negative, and every total is a magnitude."""
    outflows = inflows = paid = missed = due = 0
    missed_count = due_count = 0
    for occurrence in occurrences:
        if occurrence.expected_cents < 0:
            outflows += -occurrence.expected_cents
        else:
            inflows += occurrence.expected_cents
        if occurrence.status is OccurrenceStatus.paid:
            paid += abs(
                occurrence.expected_cents
                if occurrence.actual_cents is None
                else occurrence.actual_cents
            )
        elif occurrence.status is OccurrenceStatus.missed:
            missed += abs(occurrence.expected_cents)
            missed_count += 1
        else:
            due += abs(occurrence.expected_cents)
            due_count += 1
    return MonthSummary(outflows, inflows, paid, missed, missed_count, due, due_count)


def upcoming(session: Session, *, start: date, end: date, today: date) -> list[Occurrence]:
    """The due dates of confirmed series within [start, end], oldest first.

    Each is paid when a matching charge posted within the series' timing window of the due date,
    and missed when that window closed with no charge. Otherwise it is upcoming. Due dates start
    with the series' first matched charge, so a series does not show months before it began.
    Reads only.
    """
    rows = session.scalars(
        select(RecurringSeries)
        .where(RecurringSeries.status == SeriesStatus.confirmed)
        .order_by(RecurringSeries.id)
    ).all()
    found: list[Occurrence] = []
    for row in rows:
        if row.next_due is not None:
            found.extend(
                _occurrences(session, row, first=row.next_due, start=start, end=end, today=today)
            )
    return sorted(found, key=lambda o: (o.due_on, o.series_name, o.series_id))


def _occurrences(
    session: Session, series: RecurringSeries, *, first: date, start: date, end: date, today: date
) -> list[Occurrence]:
    """The occurrences of one series in [start, end]. `first` is its stored next due date."""
    schedule = _SCHEDULES[series.cadence]
    window = timedelta(days=schedule.tolerance_days)
    matched = series_transactions(session, series, since=None)
    if matched:
        anchor = _anchor_day([txn.posted_on for txn in matched])
        floor = matched[0].posted_on - window
    else:
        anchor = first.day
        floor = first
    dues = _due_dates(first, series.cadence, anchor, low=max(start, floor), high=end)
    used: set[int] = set()
    found: list[Occurrence] = []
    for due in dues:
        near = [
            txn
            for txn in matched
            if txn.id not in used and abs((txn.posted_on - due).days) <= schedule.tolerance_days
        ]
        if near:
            txn = min(near, key=lambda t: (abs((t.posted_on - due).days), t.posted_on))
            used.add(txn.id)
            found.append(
                Occurrence(
                    series.id,
                    series.name,
                    due,
                    series.amount_cents,
                    OccurrenceStatus.paid,
                    transaction_id=txn.id,
                    posted_on=txn.posted_on,
                    actual_cents=txn.amount_cents,
                )
            )
            continue
        status = OccurrenceStatus.missed if today > due + window else OccurrenceStatus.upcoming
        found.append(Occurrence(series.id, series.name, due, series.amount_cents, status))
    return found


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
