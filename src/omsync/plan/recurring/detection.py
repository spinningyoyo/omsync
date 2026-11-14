"""Schedule math, payee keys, detection of series, and matching of decisions to them."""

import itertools
import math
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.months import shift_months
from omsync.ledger.categories import NOT_IN_REPORTS
from omsync.ledger.models import Category, Transaction
from omsync.plan.models import (
    Cadence,
    RecurringPaidElsewhere,
    RecurringPrice,
    RecurringRejection,
    RecurringSeries,
    SeriesStatus,
)
from omsync.plan.recurring.base import (
    _MONTHLY_RATIOS,
    _OVERLAP_HIGH,
    _OVERLAP_LOW,
    _OVERLAP_SLACK_CENTS,
    _SCHEDULES,
    BAND_FLOOR_CENTS,
    BAND_RATIO,
    BUFFER_DAYS,
    DUPLICATE_WINDOW_DAYS,
    MAX_CV,
    MAX_SKIPPED_CYCLES,
    MIN_CONFIDENCE,
    MIN_OCCURRENCES,
    MIN_REGULARITY,
    MIN_TOLERANCE_CENTS,
    VARIABLE_MIN_OCCURRENCES,
    VARIABLE_MIN_REGULARITY,
    SeriesSuggestion,
    _Schedule,
)
from omsync.reconcile.text import comparable_payee


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
    base = comparable_payee(_RAW_STAR_CODE.sub(" ", payee_raw or ""))
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
    gaps = [(later - earlier).days for earlier, later in itertools.pairwise(dates)]
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
            MIN_TOLERANCE_CENTS, *(abs(amount - typical) for amount in amounts)
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
