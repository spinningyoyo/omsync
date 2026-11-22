"""Bulk edits: one change to many transactions, previewed first and applied as one batch.

A bulk edit changes one thing: the category (set, or cleared) or the memo. plan_bulk writes nothing.
It counts what would change, so the confirm step can say "Update 37 transactions; 3 split
transactions skipped". apply_bulk writes that plan as one batch, so one undo reverses all of it,
and the same idempotency key applies at most once.

A split transaction keeps its categories on its splits, so a category change skips it and counts
it. A memo is a transaction field, so a memo change applies to split transactions too.
ids_matching and count_matching give the transactions a filter selects, for "select all matching".
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    BatchError,
    Change,
    Preview,
    applied_batch_for_key,
    apply,
    make_preview,
)
from omsync.ledger.categories import Category
from omsync.ledger.filters import FilterSpec
from omsync.ledger.filters import apply as apply_filter
from omsync.ledger.tags import Tag, TransactionTag, link_changes
from omsync.ledger.transactions import CategoryOrigin, Split, Transaction


@dataclass(frozen=True)
class BulkPlan:
    """What a bulk edit would do. Nothing is written until apply_bulk.

    `kind` is "bulk.category" or "bulk.memo". `changed` transactions get a change, `skipped_split`
    split transactions were left alone by a category change, and `unchanged` already had the value.
    `preview.base_revision` is the data revision the counts were computed from.
    """

    kind: str
    label: str
    preview: Preview
    changed: int
    skipped_split: int
    unchanged: int


def plan_bulk(
    session: Session,
    txn_ids: Iterable[int],
    *,
    set_category_id: int | None = None,
    clear_category: bool = False,
    set_memo: str | None = None,
    add_tag_id: int | None = None,
    remove_tag_id: int | None = None,
) -> BulkPlan:
    """Plan one change to many transactions. Writes nothing.

    Choose exactly one change: set_category_id, clear_category, set_memo, add_tag_id, or
    remove_tag_id. A blank set_memo clears the memo. Raises ValueError for a choice that is
    missing or doubled, or for an entity that does not exist or is hidden. Raises LookupError
    for a transaction that does not exist.
    """
    ids = sorted(set(txn_ids))
    if not ids:
        raise ValueError("Select at least one transaction.")

    actions_count = sum(
        1
        for a in (
            set_category_id is not None,
            clear_category,
            set_memo is not None,
            add_tag_id is not None,
            remove_tag_id is not None,
        )
        if a
    )
    if actions_count != 1:
        raise ValueError("Choose exactly one bulk action.")

    rows = {
        row.id: row
        for row in session.execute(
            select(
                Transaction.id,
                Transaction.category_id,
                Transaction.category_origin,
                Transaction.memo,
            ).where(Transaction.id.in_(ids))
        )
    }
    missing = [txn_id for txn_id in ids if txn_id not in rows]
    if missing:
        raise LookupError(f"There is no transaction {missing[0]}.")

    if clear_category or set_category_id is not None:
        return _plan_category(session, ids, rows, set_category_id)
    if set_memo is not None:
        return _plan_memo(session, ids, rows, set_memo)
    if add_tag_id is not None:
        return _plan_tag(session, ids, add_tag_id, add=True)
    return _plan_tag(session, ids, remove_tag_id, add=False)  # type: ignore[arg-type]


def _plan_category(
    session: Session, ids: list[int], rows: dict[int, Any], category_id: int | None
) -> BulkPlan:
    if category_id is None:
        origin = CategoryOrigin.none
        name = None
    else:
        origin = CategoryOrigin.manual
        name = _usable_category_name(session, category_id)
    split_parents = set(
        session.scalars(select(Split.transaction_id).where(Split.transaction_id.in_(ids))).all()
    )

    changes: list[Change] = []
    skipped = unchanged = 0
    for txn_id in ids:
        if txn_id in split_parents:
            skipped += 1
            continue
        row = rows[txn_id]
        before = {"category_id": row.category_id, "category_origin": row.category_origin.value}
        after = {"category_id": category_id, "category_origin": origin.value}
        if before == after:
            unchanged += 1
        else:
            changes.append(Change("transaction", str(txn_id), before, after))

    changed = len(changes)
    if name is None:
        label = f"Clear category of {_count(changed)}"
    else:
        label = f"Set category of {_count(changed)} to {name}"
    return _plan(
        session,
        kind="bulk.category",
        label=label,
        changes=changes,
        field="category",
        skipped=skipped,
        unchanged=unchanged,
    )


def _plan_memo(session: Session, ids: list[int], rows: dict[int, Any], memo: str) -> BulkPlan:
    text = memo.strip() or None
    changes: list[Change] = []
    unchanged = 0
    for txn_id in ids:
        before = {"memo": rows[txn_id].memo}
        after = {"memo": text}
        if before == after:
            unchanged += 1
        else:
            changes.append(Change("transaction", str(txn_id), before, after))

    changed = len(changes)
    if text is None:
        label = f"Clear memo of {_count(changed)}"
    else:
        label = f"Set memo of {_count(changed)} to {text}"
    return _plan(
        session,
        kind="bulk.memo",
        label=label,
        changes=changes,
        field="memo",
        skipped=0,
        unchanged=unchanged,
    )


def _plan_tag(session: Session, ids: list[int], tag_id: int, *, add: bool) -> BulkPlan:
    tag = session.get(Tag, tag_id)
    if tag is None:
        raise ValueError(f"No tag found with ID {tag_id}")

    pairs = [(txn_id, tag_id) for txn_id in dict.fromkeys(ids)]
    if add:
        changes = link_changes(session, TransactionTag, add=pairs)
        label = f"Add tag '{tag.name}' to {_count(len(changes))}"
    else:
        changes = link_changes(session, TransactionTag, remove=pairs)
        label = f"Remove tag '{tag.name}' from {_count(len(changes))}"
    return _plan(
        session,
        kind="bulk.tag",
        label=label,
        changes=changes,
        field="tags",
        skipped=0,
        unchanged=len(pairs) - len(changes),
    )


def _plan(
    session: Session,
    *,
    kind: str,
    label: str,
    changes: list[Change],
    field: str,
    skipped: int,
    unchanged: int,
) -> BulkPlan:
    summary = {
        "label": label,
        "field": field,
        "changed": len(changes),
        "skipped_split": skipped,
        "unchanged": unchanged,
    }
    preview = make_preview(session, kind, changes, summary=summary)
    return BulkPlan(
        kind=kind,
        label=label,
        preview=preview,
        changed=len(changes),
        skipped_split=skipped,
        unchanged=unchanged,
    )


def apply_bulk(session: Session, plan: BulkPlan, *, idempotency_key: str) -> Batch:
    """Apply a plan as one batch. The same key applies at most once and returns the first batch.

    Raises BatchError if the plan changes nothing, since an empty batch has nothing to undo, and
    StalePreview if the data changed since the plan was made.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    if plan.changed == 0:
        raise BatchError("No transaction would change, so nothing was applied.")
    return apply(session, plan.preview, idempotency_key=idempotency_key)


def ids_matching(session: Session, spec: FilterSpec, *, account_id: int | None = None) -> list[int]:
    """The IDs of every transaction the filter selects, in ID order. `account_id` narrows it
    further, to the account the register is showing."""
    query = _matching(select(Transaction.id), spec, account_id).order_by(Transaction.id)
    return list(session.scalars(query).all())


def count_matching(session: Session, spec: FilterSpec, *, account_id: int | None = None) -> int:
    """How many transactions the filter selects. Same selection as ids_matching."""
    query = _matching(select(func.count(Transaction.id)), spec, account_id)
    return int(session.scalar(query) or 0)


def _matching(query: Select, spec: FilterSpec, account_id: int | None) -> Select:
    if account_id is not None:
        query = query.where(Transaction.account_id == account_id)
    return apply_filter(query, spec)


def _usable_category_name(session: Session, category_id: int) -> str:
    row = session.execute(
        select(Category.name, Category.hidden).where(Category.id == category_id)
    ).first()
    if row is None:
        raise ValueError(f"There is no category {category_id}.")
    if row.hidden:
        raise ValueError(f"{row.name} is hidden, so it cannot be used.")
    return str(row.name)


def _count(changed: int) -> str:
    noun = "transaction" if changed == 1 else "transactions"
    return f"{changed} {noun}"
