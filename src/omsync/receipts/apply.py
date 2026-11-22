"""Plan and apply receipt categories, splits, and links.

AUTOINCREMENT keeps deleted split IDs distinct from new ones, so undo can identify the
rows it must restore even when application replaces all splits.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    BatchError,
    Change,
    Preview,
    applied_batch_for_key,
    apply,
    make_preview,
    snapshot,
)
from omsync.ledger.categorize import load_keywords, match_receipt_item
from omsync.ledger.models import CategoryOrigin, Split, Transaction
from omsync.ledger.receipt_vendors import code_categories
from omsync.ledger.tags import split_tag_deletes
from omsync.receipts.allocate import (
    AllocationError,
    AllocationLine,
    CategoryForItem,
    allocate_charge,
)
from omsync.receipts.match import MatchOutcome, MatchReport, MatchStatus
from omsync.receipts.model import ReceiptItem

YOU_SET_IT = "you set this category"


@dataclass(frozen=True)
class ApplyItem:
    """A matched charge that will be applied, and the lines it becomes."""

    receipt_key: str
    transaction_id: int
    lines: tuple[AllocationLine, ...]


@dataclass(frozen=True)
class ApplySkip:
    """A matched charge that will not be applied, and why."""

    receipt_key: str
    transaction_id: int | None
    reason: str
    item_positions: tuple[int, ...] = ()
    offset_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApplyPlan:
    preview: Preview | None
    applied: tuple[ApplyItem, ...]
    skipped: tuple[ApplySkip, ...]


def keyword_categories(session: Session, vendor_key: str) -> CategoryForItem:
    """Categorize receipt items with one vendor's keyword list, as categorize does for payees. An
    item that no keyword matches takes the category set for its vendor code, if there is one."""
    keywords = load_keywords(session)
    by_code = code_categories(session, vendor_key)

    def category_for_item(item: ReceiptItem) -> int | None:
        match = match_receipt_item(vendor_key, item.description, keywords)
        if match is not None:
            return match.category_id
        return None if item.code is None else by_code.get(item.code)

    return category_for_item


def plan_apply(
    session: Session, report: MatchReport, category_for_item: CategoryForItem
) -> ApplyPlan:
    """Plan the matched charges of a report. Writes nothing."""
    applied: list[ApplyItem] = []
    skipped: list[ApplySkip] = []
    changes: list[Change] = []
    claimed: set[int] = set()
    for outcome in report.with_status(MatchStatus.matched):
        tx_id = outcome.transaction_id
        if tx_id is None:  # Matched charges always name a transaction.
            raise BatchError("a matched charge has no transaction")
        if tx_id in claimed:
            skipped.append(_skip(outcome, "another receipt charge in this plan claims it"))
            continue
        result = _plan_one(session, outcome, category_for_item)
        if isinstance(result, ApplySkip):
            skipped.append(result)
            continue
        item, item_changes = result
        claimed.add(tx_id)
        applied.append(item)
        changes.extend(item_changes)

    preview = (
        make_preview(
            session,
            "receipts",
            changes,
            summary={"vendor": report.vendor_key, "applied": len(applied), "skipped": len(skipped)},
        )
        if applied
        else None
    )
    return ApplyPlan(preview, tuple(applied), tuple(skipped))


def apply_receipts(session: Session, plan: ApplyPlan, *, idempotency_key: str) -> Batch:
    """Write a plan's preview as one batch. The same key applies at most once.

    Raises BatchError if nothing would change, and StalePreview if the data changed since the
    plan was made.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    if plan.preview is None:
        raise BatchError("nothing to apply: no receipt charge would change")
    return apply(session, plan.preview, idempotency_key=idempotency_key)


def _plan_one(
    session: Session,
    outcome: MatchOutcome,
    category_for_item: CategoryForItem,
) -> ApplySkip | tuple[ApplyItem, list[Change]]:
    receipt, charge = outcome.receipt, outcome.charge
    if charge is None or outcome.transaction_id is None:
        return _skip(outcome, "the match has no charge")
    tx = session.get(Transaction, outcome.transaction_id)
    if tx is None:
        return _skip(outcome, "the transaction no longer exists")
    splits = list(
        session.scalars(select(Split).where(Split.transaction_id == tx.id).order_by(Split.position))
    )
    origins = {CategoryOrigin(tx.category_origin)} | {
        CategoryOrigin(split.category_origin) for split in splits
    }
    if CategoryOrigin.manual in origins:
        return _skip(outcome, YOU_SET_IT)

    try:
        result = allocate_charge(receipt, charge, category_for_item)
    except AllocationError as exc:
        return _skip(outcome, str(exc))
    if result.status == "needs_triage":
        n_items = len(result.triage_positions) + len(result.triage_offsets)
        return _skip(
            outcome,
            f"{n_items} receipt item(s) need a category first",
            result.triage_positions,
            result.triage_offsets,
        )

    lines = result.lines
    changes: list[Change] = split_tag_deletes(session, [split.id for split in splits])
    changes += [
        Change("split", str(split.id), snapshot(session, "split", split.id), None)
        for split in splits
    ]
    parent_before = {
        "category_id": tx.category_id,
        "category_origin": CategoryOrigin(tx.category_origin).value,
    }
    if len(lines) == 1:
        parent_after = {
            "category_id": lines[0].category_id,
            "category_origin": CategoryOrigin.receipt.value,
        }
    else:
        parent_after = {"category_id": None, "category_origin": CategoryOrigin.none.value}
    if parent_after != parent_before:
        changes.append(Change("transaction", str(tx.id), parent_before, parent_after))
    if len(lines) > 1:
        changes.extend(
            Change(
                "split",
                None,
                None,
                {
                    "transaction_id": tx.id,
                    "position": position,
                    "amount_cents": line.amount_cents,
                    "category_id": line.category_id,
                    "category_origin": CategoryOrigin.receipt.value,
                    "memo": line.memo,
                },
            )
            for position, line in enumerate(lines)
        )
    changes.append(
        Change(
            "link",
            None,
            None,
            {
                "kind": "receipt",
                # A link is a positive magnitude of the amount it explains. The sign stays on the
                # transaction and its splits, so a purchase's receipt link is positive too.
                "amount_cents": abs(charge.amount_cents),
                "to_transaction_id": tx.id,
                "receipt_key": charge.receipt_key,
                "evidence": {
                    "vendor": receipt.vendor_key,
                    "receipt_id": receipt.receipt_id,
                    "revision": receipt.revision,
                    "account": receipt.account,
                    "observed": charge.observed,
                },
            },
        )
    )
    item = ApplyItem(charge.receipt_key, tx.id, lines)
    return item, changes


def _skip(
    outcome: MatchOutcome,
    reason: str,
    item_positions: tuple[int, ...] = (),
    offset_names: tuple[str, ...] = (),
) -> ApplySkip:
    return ApplySkip(
        receipt_key=outcome.receipt_key,
        transaction_id=outcome.transaction_id,
        reason=reason,
        item_positions=item_positions,
        offset_names=offset_names,
    )
