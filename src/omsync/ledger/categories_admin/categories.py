"""Category writes: create, update, hide, delete, and move."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    Change,
    Preview,
    StalePreview,
    applied_batch_for_key,
    apply,
    make_preview,
    next_primary_key,
    preview_digest,
)
from omsync.ledger.categories_admin.helpers import (
    _category_snapshot,
    _category_type,
    _category_usage,
    _check_no_clash,
    _clean_name,
    _costco_food_changes,
    _ensure_name_free,
    _icon,
    _rental_mark_changes,
    _repoint_changes,
    _rule_changes,
    _saved_filter_changes,
    _write,
)
from omsync.ledger.categories_admin.reads import category_uses
from omsync.ledger.categories_admin.rows import (
    IN_USE,
    NOTHING_CHANGED,
    STALE,
    CategoryUse,
    NothingChanged,
)
from omsync.ledger.models import (
    Category,
    CategoryType,
)


def create_category(
    session: Session,
    name: str,
    category_type: CategoryType | str,
    *,
    idempotency_key: str,
) -> Batch:
    """Add a category. Names are unique without regard to case. Raises ValueError for a clash."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    clean_name = _clean_name(name)
    chosen = _category_type(category_type)
    _ensure_name_free(session, clean_name, except_id=None)
    new_id = next_primary_key(session, Category.__table__)
    values = {
        "id": new_id,
        "name": clean_name,
        "type": chosen.value,
        "hidden": False,
        "icon": None,
    }
    return _write(
        session,
        "category.create",
        [Change("category", str(new_id), None, values)],
        f"Created category {clean_name}",
        idempotency_key,
        category_id=new_id,
    )


def update_category(
    session: Session, category_id: int, *, idempotency_key: str, **fields: Any
) -> Batch:
    """Change a category's name, type, or icon. An icon of None or "" restores the default.

    Raises LookupError if it does not exist, ValueError for a bad value, and NothingChanged when
    the values are already stored.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _category_snapshot(session, category_id)
    after = dict(before)
    for field, value in fields.items():
        if field == "name":
            after["name"] = _clean_name(value)
        elif field == "type":
            after["type"] = _category_type(value).value
        elif field == "icon":
            after["icon"] = _icon(value)
        else:
            raise ValueError(f"A category has no field {field!r} that can be changed here.")
    if after == before:
        raise NothingChanged(NOTHING_CHANGED)
    if after["name"] != before["name"]:
        _ensure_name_free(session, after["name"], except_id=category_id)
    return _write(
        session,
        "category.update",
        [Change("category", str(category_id), before, after)],
        f"Updated category {before['name']}",
        idempotency_key,
        category_id=category_id,
    )


def bulk_update_categories(
    session: Session,
    category_ids: Sequence[int],
    *,
    category_type: CategoryType | str | None = None,
    hidden: bool | None = None,
    idempotency_key: str,
) -> Batch:
    """Set the type, or the hidden state, on several categories, as one batch.

    A field left as None is not changed. Raises ValueError when nothing was selected or asked for
    or a value is bad, LookupError for an unknown category, and NothingChanged when every
    selected category already has the values.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    ids = list(dict.fromkeys(category_ids))
    if not ids:
        raise ValueError("Select at least one category.")
    if category_type is None and hidden is None:
        raise ValueError("Choose a type to set.")
    chosen = None if category_type is None else _category_type(category_type)
    changes: list[Change] = []
    for category_id in ids:
        before = _category_snapshot(session, category_id)
        after = dict(before)
        if chosen is not None:
            after["type"] = chosen.value
        if hidden is not None:
            after["hidden"] = bool(hidden)
        if after != before:
            changes.append(Change("category", str(category_id), before, after))
    if not changes:
        raise NothingChanged(NOTHING_CHANGED)
    count = len(changes)
    label = f"Updated {count} categor{'y' if count == 1 else 'ies'}"
    return _write(
        session, "category.bulk_update", changes, label, idempotency_key, categories=count
    )


def hide_category(
    session: Session, category_id: int, hidden: bool, *, idempotency_key: str
) -> Batch:
    """Hide a category from pickers, or show it again. Existing transactions keep it."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _category_snapshot(session, category_id)
    after = {**before, "hidden": bool(hidden)}
    if after == before:
        raise NothingChanged(NOTHING_CHANGED)
    label = f"Hid category {before['name']}" if hidden else f"Showed category {before['name']}"
    return _write(
        session,
        "category.hide" if hidden else "category.show",
        [Change("category", str(category_id), before, after)],
        label,
        idempotency_key,
        category_id=category_id,
    )


def delete_category(session: Session, category_id: int, *, idempotency_key: str) -> Batch:
    """Delete a category that nothing uses. Raises ValueError("in use: hide it instead") if a
    transaction, split, keyword, rule, saved filter, or any other row still
    points at it. delete_and_move deletes one that is in use."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _category_snapshot(session, category_id)
    if category_id in _category_usage(session):
        raise ValueError(IN_USE)
    return _write(
        session,
        "category.delete",
        [Change("category", str(category_id), before, None)],
        f"Deleted category {before['name']}",
        idempotency_key,
        category_id=category_id,
    )


@dataclass(frozen=True)
class MovePreview:
    """What delete_and_move would write, computed now. `uses` is what points at the category today.
    `digest` identifies this exact operation, so an apply made from a stale preview is refused."""

    category_id: int
    category_name: str
    target_id: int
    target_name: str
    uses: tuple[CategoryUse, ...]
    digest: str
    preview: Preview


def move_preview(session: Session, category_id: int, target_id: int) -> MovePreview:
    """The changes that delete_and_move would make: every use moved to the target, then the category
    deleted. Writes nothing.

    Raises LookupError if the category does not exist. Raises ValueError, with a message for the
    screen, when the move is refused: the target is the category itself or does not exist, the
    target already has a row that would clash with one of the category's, such as a budget line for
    the same month and kind.
    """
    source = _category_snapshot(session, category_id)
    if target_id == category_id:
        raise ValueError("Pick a different category to move its uses to.")
    target = session.get(Category, target_id)
    if target is None:
        raise ValueError("Pick a category to move its uses to.")
    source_name = str(source["name"])
    _check_no_clash(session, category_id, target_id)
    changes = [
        *_rule_changes(session, category_id, target_id),
        *_repoint_changes(session, category_id, target_id),
        *_saved_filter_changes(session, category_id, target_id),
        *_rental_mark_changes(session, category_id, target_id),
        *_costco_food_changes(session, category_id, target_id),
        Change("category", str(category_id), source, None),
    ]
    label = f"Deleted {source_name} and moved its uses to {target.name}"
    preview = make_preview(
        session,
        "category.delete_move",
        changes,
        summary={
            "label": label,
            "category_id": category_id,
            "target_id": target_id,
        },
    )
    return MovePreview(
        category_id=category_id,
        category_name=source_name,
        target_id=target_id,
        target_name=target.name,
        uses=tuple(category_uses(session, category_id)),
        digest=preview_digest(preview),
        preview=preview,
    )


def delete_and_move(
    session: Session,
    category_id: int,
    target_id: int,
    *,
    idempotency_key: str,
    digest: str | None = None,
) -> Batch:
    """Move every use of a category to another one, then delete the category, as one batch that undo
    reverses. Keywords move as they are, transactions and splits keep their origin, and a rental
    category makes the target one. See move_preview for what is refused.

    `digest` is the digest of the preview the person saw. When it no longer matches the move,
    nothing is written and StalePreview is raised. The same key applies at most once.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    moving = move_preview(session, category_id, target_id)
    if digest is not None and digest != moving.digest:
        raise StalePreview(STALE)
    return apply(session, moving.preview, idempotency_key=idempotency_key)
