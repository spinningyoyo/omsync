"""Keyword writes."""

from typing import Any

from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    Change,
    applied_batch_for_key,
    next_primary_key,
)
from omsync.ledger.categories_admin.helpers import (
    _check_category,
    _check_list_key,
    _clean_pattern,
    _ensure_pattern_free,
    _keyword_snapshot,
    _write,
)
from omsync.ledger.categories_admin.rows import NOTHING_CHANGED, NothingChanged
from omsync.ledger.categorize import (
    list_name,
)
from omsync.ledger.models import (
    Keyword,
)


def add_keyword(
    session: Session,
    list_key: str,
    pattern: str,
    category_id: int,
    *,
    is_regex: bool = True,
    idempotency_key: str,
) -> Batch:
    """Add a keyword to a list. The pattern must not be blank or a bad regex, and it must not repeat
    a pattern already in this list, ignoring case. Raises ValueError for any of these."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    _check_list_key(list_key)
    clean = _clean_pattern(pattern, is_regex=is_regex)
    _check_category(session, category_id)
    _ensure_pattern_free(session, list_key, clean, except_id=None)
    new_id = next_primary_key(session, Keyword.__table__)
    values = {
        "id": new_id,
        "list_key": list_key,
        "pattern": clean,
        "is_regex": bool(is_regex),
        "category_id": category_id,
        "enabled": True,
    }
    return _write(
        session,
        "keyword.add",
        [Change("keyword", str(new_id), None, values)],
        f'Added keyword "{clean}" to {list_name(list_key)}',
        idempotency_key,
        keyword_id=new_id,
    )


def update_keyword(
    session: Session, keyword_id: int, *, idempotency_key: str, **fields: Any
) -> Batch:
    """Change a keyword's pattern, is_regex, or category_id. Enabling is separate.

    Raises LookupError if it does not exist, ValueError for a bad value, and NothingChanged when
    the values are already stored.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    unknown = set(fields) - {"pattern", "is_regex", "category_id"}
    if unknown:
        raise ValueError(f"A keyword has no field {min(unknown)!r} that can be changed here.")
    before = _keyword_snapshot(session, keyword_id)
    is_regex = bool(fields.get("is_regex", before["is_regex"]))
    pattern = _clean_pattern(fields.get("pattern", before["pattern"]), is_regex=is_regex)
    category_id = fields.get("category_id", before["category_id"])
    _check_category(session, category_id)
    after = {
        **before,
        "pattern": pattern,
        "is_regex": is_regex,
        "category_id": category_id,
    }
    if after == before:
        raise NothingChanged(NOTHING_CHANGED)
    _ensure_pattern_free(session, before["list_key"], pattern, except_id=keyword_id)
    return _write(
        session,
        "keyword.update",
        [Change("keyword", str(keyword_id), before, after)],
        f'Updated keyword "{pattern}"',
        idempotency_key,
        keyword_id=keyword_id,
    )


def set_keyword_enabled(
    session: Session, keyword_id: int, enabled: bool, *, idempotency_key: str
) -> Batch:
    """Turn a keyword on or off. A disabled keyword is ignored by matching and conflicts."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _keyword_snapshot(session, keyword_id)
    after = {**before, "enabled": bool(enabled)}
    if after == before:
        raise NothingChanged(NOTHING_CHANGED)
    verb = "Turned on" if enabled else "Turned off"
    label = f'{verb} keyword "{before["pattern"]}"'
    return _write(
        session,
        "keyword.enable" if enabled else "keyword.disable",
        [Change("keyword", str(keyword_id), before, after)],
        label,
        idempotency_key,
        keyword_id=keyword_id,
    )


def delete_keyword(session: Session, keyword_id: int, *, idempotency_key: str) -> Batch:
    """Delete a keyword. Undo restores it with its id."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _keyword_snapshot(session, keyword_id)
    return _write(
        session,
        "keyword.delete",
        [Change("keyword", str(keyword_id), before, None)],
        f'Deleted keyword "{before["pattern"]}"',
        idempotency_key,
        keyword_id=keyword_id,
    )
