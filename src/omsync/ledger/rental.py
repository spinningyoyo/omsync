"""Rental settings for the rental export: which accounts are rental properties, with the name each
has in the report, and which categories count as rental expenses.

A property's label is on its account, and a rental category is marked on the category, so a
renamed category stays a rental category and a deleted one takes its mark with it. Each change is
one batch that you can undo.
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import Change, applied_batch_for_key, apply, make_preview, snapshot
from omsync.ledger.models import Account, Category

LABEL_MAX = 80


def get_rental_accounts(session: Session) -> dict[int, str]:
    """Rental account IDs mapped to their property labels, by label."""
    rows = session.execute(
        select(Account.id, Account.rental_label).where(Account.rental_label.is_not(None))
    ).all()
    return {
        account_id: str(label)
        for account_id, label in sorted(rows, key=lambda row: (str(row[1]).casefold(), row[0]))
    }


def get_rental_categories(session: Session) -> list[str]:
    """The names of the categories that count as rental expenses, by name."""
    names = session.scalars(select(Category.name).where(Category.rental.is_(True)))
    return sorted(names, key=str.casefold)


def set_rental_account(
    session: Session, account_id: int, label: str, *, idempotency_key: str
) -> Batch:
    """Mark an account as a rental property with this label, or relabel it, as one batch.

    Raises LookupError when there is no such account, and ValueError for a blank or too-long label,
    or when the label is already the account's own. The same key applies at most once.
    """
    name = label.strip()
    if not name:
        raise ValueError("A rental property needs a label.")
    if len(name) > LABEL_MAX:
        raise ValueError(f"A label has at most {LABEL_MAX} characters.")
    before = snapshot(session, "account", account_id)
    if before is None:
        raise LookupError(f"There is no account {account_id}.")
    was = before.get("rental_label")
    summary = f"Renamed the property to {name}" if was else f"Added {name} as a rental property"
    return _write_account(session, before, name, summary, idempotency_key)


def remove_rental_account(session: Session, account_id: int, *, idempotency_key: str) -> Batch:
    """Stop treating an account as a rental property, as one batch. Its transactions stay as they
    are. Raises LookupError when the account is not a rental property."""
    before = snapshot(session, "account", account_id)
    if before is None or before.get("rental_label") is None:
        raise LookupError("That account is not a rental property.")
    summary = f"Removed {before['rental_label']} from rental properties"
    return _write_account(session, before, None, summary, idempotency_key)


def set_rental_categories(session: Session, names: Sequence[str], *, idempotency_key: str) -> Batch:
    """Make exactly these categories the rental categories, as one batch.

    Raises ValueError when no name is given, a name is not a category, or nothing would change.
    """
    wanted = {name.strip() for name in names if name.strip()}
    if not wanted:
        raise ValueError("Give at least one rental category.")
    by_name = {category.name: category for category in session.scalars(select(Category))}
    unknown = sorted(wanted - set(by_name), key=str.casefold)
    if unknown:
        raise ValueError(f"There is no category named {', '.join(unknown)}.")
    ids = [
        category.id for category in by_name.values() if category.rental != (category.name in wanted)
    ]
    label = f"Set the rental categories to {', '.join(sorted(wanted, key=str.casefold))}"
    return _write_categories(session, ids, label, idempotency_key)


def set_rental_category(
    session: Session, category_id: int, rental: bool, *, idempotency_key: str
) -> Batch:
    """Count a category as a rental expense, or stop counting it, as one batch.

    Raises LookupError when there is no such category, and ValueError when it is already so, or
    when it is the last rental category.
    """
    category = session.get(Category, category_id)
    if category is None:
        raise LookupError(f"There is no category {category_id}.")
    if not rental and category.rental and len(get_rental_categories(session)) == 1:
        raise ValueError("Keep at least one rental category.")
    if category.rental == rental:
        raise ValueError("Nothing has changed.")
    verb = "Added" if rental else "Removed"
    where = "to" if rental else "from"
    return _write_categories(
        session, [category_id], f"{verb} {category.name} {where} rental categories", idempotency_key
    )


def _write_account(
    session: Session,
    before: dict[str, object],
    label: str | None,
    summary: str,
    idempotency_key: str,
) -> Batch:
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    if before.get("rental_label") == label:
        raise ValueError("Nothing has changed.")
    change = Change("account", str(before["id"]), before, {**before, "rental_label": label})
    preview = make_preview(
        session,
        "rental.account",
        [change],
        summary={"label": summary, "account_id": before["id"]},
    )
    return apply(session, preview, idempotency_key=idempotency_key)


def _write_categories(
    session: Session, category_ids: Sequence[int], label: str, idempotency_key: str
) -> Batch:
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    if not category_ids:
        raise ValueError("Nothing has changed.")
    changes = []
    for category_id in category_ids:
        before = snapshot(session, "category", category_id)
        if before is None:
            raise LookupError(f"There is no category {category_id}.")
        changes.append(
            Change("category", str(category_id), before, {**before, "rental": not before["rental"]})
        )
    preview = make_preview(session, "rental.category", changes, summary={"label": label})
    return apply(session, preview, idempotency_key=idempotency_key)


__all__ = [
    "get_rental_accounts",
    "get_rental_categories",
    "remove_rental_account",
    "set_rental_account",
    "set_rental_categories",
    "set_rental_category",
]
