"""The receipts service: read a vendor's folder, match its receipts, and apply them as one batch.

run_vendor reads <data_dir>/receipts/<VENDOR KEY>/ through the vendor, matches every charge to
a transaction, and plans the apply. It writes nothing. The report lists the outcome counts, the
lines to review, and the plan. apply_report writes that plan as one batch.

A period (start and end) limits the plan to matched charges whose transaction posted inside it.
Matching still covers all history, so a transaction two charges claim, or one that fits two
charges, stays ambiguous whichever period is chosen. apply_digest binds a preview to its period.

A vendor raises ValueError for a file it cannot read. That file goes to review, and the other
files are still used. A receipt that two files describe differently is excluded and reviewed.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core import money
from omsync.core.batch_models import Batch
from omsync.core.batches import preview_digest
from omsync.core.settings import Settings
from omsync.ledger.models import Transaction
from omsync.receipts.apply import (
    ApplyPlan,
    ApplySkip,
    apply_receipts,
    keyword_categories,
    plan_apply,
)
from omsync.receipts.match import MatchOutcome, MatchReport, MatchStatus, match_receipts
from omsync.receipts.model import Charge, Receipt
from omsync.receipts.vendor import ReceiptVendor, get_vendor
from omsync.receipts.vendors import BUILT_IN_VENDORS, ensure_registered

REVIEW_STATUSES = (
    MatchStatus.ambiguous,
    MatchStatus.unmatched,
    MatchStatus.unknown_payer,
    MatchStatus.insufficient,
    MatchStatus.changed_since_applied,
)


@dataclass(frozen=True)
class ReviewLine:
    """One thing to look at. kind is a MatchStatus value, needs_triage, or parse_error."""

    kind: str
    text: str


@dataclass(frozen=True)
class ServiceReport:
    vendor_key: str
    folder: Path
    files_read: int
    receipts_read: int
    counts: dict[str, int]
    review: tuple[ReviewLine, ...]
    plan: ApplyPlan
    start: date | None = None  # the period the plan is limited to, or None for all history
    end: date | None = None

    @property
    def to_apply(self) -> int:
        return len(self.plan.applied)


def registered_vendors() -> tuple[ReceiptVendor, ...]:
    """The vendors omsync reads, in the order they are listed."""
    return BUILT_IN_VENDORS


def vendor_folder(settings: Settings, vendor_key: str) -> Path:
    """Where a vendor's exports live: <data_dir>/receipts/<VENDOR KEY>/."""
    return settings.receipts_dir / vendor_key.upper()


def run_vendor(
    session: Session,
    settings: Settings,
    vendor_key: str,
    *,
    today: date,
    start: date | None = None,
    end: date | None = None,
) -> ServiceReport:
    """Read and match a vendor's receipts, and plan the apply. Writes nothing.

    With start and end (both, inclusive, or neither for all history), only matched charges whose
    transaction posted inside the period are planned. Counts and review lines still cover all
    history, and the outside_period count says how many matched charges were left out.

    Raises KeyError for an unknown vendor key, and ValueError for a half period or start after end.
    A missing folder is not an error: it has no files.
    """
    if (start is None) != (end is None):
        raise ValueError("give start and end together, or neither")
    if start is not None and end is not None and start > end:
        raise ValueError("start is after end")
    ensure_registered()
    vendor = get_vendor(vendor_key)
    folder = vendor_folder(settings, vendor_key)
    files = vendor.discover(folder) if folder.is_dir() else []
    receipts, parse_errors = _read_receipts(vendor, folder, files)
    report = match_receipts(session, vendor, receipts, today=today)
    in_period, outside = _in_period(session, report, start, end)
    plan = plan_apply(session, in_period, keyword_categories(session, vendor_key))
    return ServiceReport(
        vendor_key=vendor_key,
        folder=folder,
        files_read=len(files),
        receipts_read=len(receipts),
        counts=_counts(report, plan, outside),
        review=tuple([*parse_errors, *_review_lines(session, report, plan)]),
        plan=plan,
        start=start,
        end=end,
    )


def apply_digest(report: ServiceReport) -> str:
    """A digest of what apply would write, and of the period it was planned for.

    The page shows it, and apply refuses when the digest it is given differs. A preview made for
    one period therefore never applies in another, even when both plan the same changes.
    """
    if report.start is None or report.end is None:
        scope = "all"
    else:
        scope = f"{report.start.isoformat()}/{report.end.isoformat()}"
    return hashlib.sha256(f"{scope}\n{preview_digest(report.plan.preview)}".encode()).hexdigest()


def apply_report(session: Session, report: ServiceReport, *, idempotency_key: str) -> Batch | None:
    """Write a report's plan as one batch. Returns None when nothing would change.

    The same key applies at most once. Raises BatchError (StalePreview among them) when the
    data changed after the report was made.
    """
    if report.plan.preview is None:
        return None
    return apply_receipts(session, report.plan, idempotency_key=idempotency_key)


def _read_receipts(
    vendor: ReceiptVendor, folder: Path, files: Iterable[Path]
) -> tuple[list[Receipt], list[ReviewLine]]:
    """Parse every file. One receipt ID is one receipt; a conflicting copy excludes it."""
    by_id: dict[str, Receipt] = {}
    conflicts: set[str] = set()
    errors: list[ReviewLine] = []
    for path in files:
        label = path.relative_to(folder).as_posix()
        try:
            parsed = vendor.parse(path)
        except ValueError as exc:
            errors.append(ReviewLine("parse_error", f"{label}: {exc}"))
            continue
        for receipt in parsed:
            known = by_id.get(receipt.receipt_id)
            if known is None:
                by_id[receipt.receipt_id] = receipt
            elif known.revision != receipt.revision:
                conflicts.add(receipt.receipt_id)
    for receipt_id in sorted(conflicts):
        by_id.pop(receipt_id, None)
        errors.append(ReviewLine("parse_error", f"{receipt_id}: two files describe it differently"))
    return list(by_id.values()), errors


def _in_period(
    session: Session, report: MatchReport, start: date | None, end: date | None
) -> tuple[MatchReport, int]:
    """The report without the matched charges that posted outside the period, and how many.

    Only matched charges are dropped: review outcomes are kept, because they never apply.
    """
    matched = report.with_status(MatchStatus.matched)
    if start is None or end is None or not matched:
        return report, 0
    posted = dict(
        session.execute(
            select(Transaction.id, Transaction.posted_on).where(
                Transaction.id.in_([outcome.transaction_id for outcome in matched])
            )
        ).all()
    )
    kept = tuple(
        outcome
        for outcome in report.outcomes
        if outcome.status is not MatchStatus.matched
        or start <= posted[outcome.transaction_id] <= end
    )
    kept_matched = sum(1 for outcome in kept if outcome.status is MatchStatus.matched)
    return replace(report, outcomes=kept), len(matched) - kept_matched


def _counts(report: MatchReport, plan: ApplyPlan, outside: int) -> dict[str, int]:
    counts = {status.value: number for status, number in report.counts.items()}
    counts["outside_period"] = outside
    counts["will_apply"] = len(plan.applied)
    counts["apply_skipped"] = len(plan.skipped)
    return counts


def _review_lines(session: Session, report: MatchReport, plan: ApplyPlan) -> list[ReviewLine]:
    lines: list[ReviewLine] = []
    for status in REVIEW_STATUSES:
        for outcome in report.with_status(status):
            reason = outcome.reason or ""
            if status is MatchStatus.unknown_payer:
                reason = _unknown_payer_reason(session, outcome)
            lines.append(ReviewLine(status.value, _describe(outcome, reason)))
    by_key = {outcome.receipt_key: outcome for outcome in report.outcomes}
    for skip in plan.skipped:
        outcome = by_key[skip.receipt_key]
        lines.append(ReviewLine("needs_triage", _describe_skip(outcome, skip)))
    return lines


def _unknown_payer_reason(session: Session, outcome: MatchOutcome) -> str:
    """The reason, naming the transactions the charge might be, so they can be categorized."""
    reason = outcome.reason or ""
    if not outcome.candidate_ids:
        return f"{reason}; no transaction with this amount is in the window"
    rows = session.execute(
        select(Transaction.id, Transaction.posted_on, Transaction.payee)
        .where(Transaction.id.in_(outcome.candidate_ids))
        .order_by(Transaction.posted_on, Transaction.id)
    ).all()
    named = " or ".join(f"#{row.id} on {row.posted_on.isoformat()} at {row.payee}" for row in rows)
    return f"{reason}; it may be {named}"


def _describe_skip(outcome: MatchOutcome, skip: ApplySkip) -> str:
    """The skip reason, naming the receipt items that need a category when there are some."""
    names = [
        item.description for item in outcome.receipt.items if item.position in skip.item_positions
    ]
    detail = f"{skip.reason}: {', '.join(names)}" if names else skip.reason
    return _describe(outcome, detail)


def _describe(outcome: MatchOutcome, reason: str) -> str:
    """One safe line: date, amount, vendor login, order, and reason. Never a card number."""
    receipt = outcome.receipt
    amount = _amount(outcome.charge, receipt)
    return (
        f"{receipt.purchased_on.isoformat()}  {money.format_cents(amount)}  "
        f"{receipt.vendor_login}  {receipt.receipt_id}  {reason}"
    )


def _amount(charge: Charge | None, receipt: Receipt) -> int:
    if charge is not None:
        return charge.amount_cents
    return receipt.total_cents if receipt.is_refund else -receipt.total_cents
