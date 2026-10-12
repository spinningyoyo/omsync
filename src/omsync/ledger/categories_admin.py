"""Admin writes for categories, keywords, and amount rules, and the reads the Categories page needs.

Every write is one batch from omsync.core.batches, with an idempotency key. A key that already
applied returns its batch, even if the data has moved on, so a retried form changes nothing twice.
A refused change raises ValueError with a message for the screen, and nothing is written.

Triage reads receipt files through the vendor registry. This module never imports omsync.web.
"""

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    Change,
    applied_batch_for_key,
    apply,
    make_preview,
    next_primary_key,
    snapshot,
)
from omsync.core.db import Base
from omsync.core.money import format_cents, to_cents
from omsync.core.settings import Settings
from omsync.ledger.categories import DEFAULT_CATEGORY_ICON
from omsync.ledger.categorize import (
    PAYEE_LISTS,
    compile_pattern,
    is_list_key,
    load_keywords,
    match_receipt_item,
)
from omsync.ledger.models import (
    AmountRule,
    Category,
    CategoryOrigin,
    CategoryType,
    Keyword,
    Transaction,
)
from omsync.receipts import vendor as vendor_registry

NOTHING_CHANGED = "Nothing has changed."
IN_USE = "in use: hide it instead"
_ICON_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_WORD = re.compile(r"[^\W\d_]+")  # letters of any script, never digits or underscores


class NothingChanged(ValueError):
    """An update asked for the values that are already stored. Nothing was written."""


@dataclass(frozen=True)
class CategoryRow:
    id: int
    name: str
    type: str  # the CategoryType value
    hidden: bool
    icon: str  # a Lucide icon name; the default when none was chosen


@dataclass(frozen=True)
class KeywordRow:
    id: int
    list_key: str
    pattern: str
    is_regex: bool
    priority: int
    enabled: bool
    category_id: int
    category_name: str
    category_hidden: bool


@dataclass(frozen=True)
class AmountRuleRow:
    id: int
    list_key: str
    below_cents: int
    enabled: bool
    from_category_id: int
    from_category_name: str
    from_category_hidden: bool
    to_category_id: int
    to_category_name: str
    to_category_hidden: bool


# Reads


CATEGORY_SORTS = ("name", "type")


def category_rows(
    session: Session,
    *,
    include_hidden: bool,
    category_type: CategoryType | None = None,
    sort: str = "name",
    descending: bool = False,
) -> list[CategoryRow]:
    """Categories, optionally of one type, sorted by name or by type. Names sort without regard to
    case. Types sort in the order of CategoryType, and a tie goes to the name, in either direction.
    Raises ValueError for another sort."""
    if sort not in CATEGORY_SORTS:
        raise ValueError(f"Categories sort by {' or '.join(CATEGORY_SORTS)}, not {sort!r}.")
    query = select(Category)
    if not include_hidden:
        query = query.where(Category.hidden.is_(False))
    if category_type is not None:
        query = query.where(Category.type == category_type)
    rows = [_category_row(row) for row in session.scalars(query)]
    if sort == "name":
        return sorted(rows, key=lambda row: (row.name.casefold(), row.id), reverse=descending)
    order = list(CategoryType)
    sign = -1 if descending else 1
    return sorted(
        rows,
        key=lambda row: (sign * order.index(CategoryType(row.type)), row.name.casefold(), row.id),
    )


def get_category(session: Session, category_id: int) -> CategoryRow:
    """Return one category. Raises LookupError if it does not exist."""
    row = session.get(Category, category_id)
    if row is None:
        raise LookupError(f"There is no category {category_id}.")
    return _category_row(row)


def category_names(session: Session) -> dict[int, str]:
    """Every category's name by id, hidden ones included, for naming what a keyword points at."""
    return dict(session.execute(select(Category.id, Category.name)).all())


def category_choices(session: Session) -> list[tuple[int, str]]:
    """(id, name) for the categories a keyword may point at: not hidden, sorted by name."""
    rows = session.execute(
        select(Category.id, Category.name).where(Category.hidden.is_(False))
    ).all()
    return sorted(((int(cid), name) for cid, name in rows), key=lambda row: row[1].casefold())


def keywords_in_list(session: Session, list_key: str) -> list[KeywordRow]:
    """The keywords of one list, in precedence order: priority, then longer pattern, then id.

    Disabled keywords are listed too, at their precedence, so you can turn them back on.
    """
    rows = session.execute(
        select(Keyword, Category.name, Category.hidden)
        .join(Category, Category.id == Keyword.category_id)
        .where(Keyword.list_key == list_key)
    ).all()
    found = [
        KeywordRow(
            id=keyword.id,
            list_key=keyword.list_key,
            pattern=keyword.pattern,
            is_regex=keyword.is_regex,
            priority=keyword.priority,
            enabled=keyword.enabled,
            category_id=keyword.category_id,
            category_name=name,
            category_hidden=hidden,
        )
        for keyword, name, hidden in rows
    ]
    return sorted(found, key=lambda row: (-row.priority, -len(row.pattern), row.id))


def get_keyword(session: Session, keyword_id: int) -> KeywordRow:
    """Return one keyword with its category. Raises LookupError if it does not exist."""
    keyword = session.get(Keyword, keyword_id)
    if keyword is None:
        raise LookupError(f"There is no keyword {keyword_id}.")
    for row in keywords_in_list(session, keyword.list_key):
        if row.id == keyword_id:
            return row
    raise LookupError(f"There is no keyword {keyword_id}.")  # pragma: no cover - same query


def amount_rules(session: Session) -> list[AmountRuleRow]:
    """Every amount rule with its category names, by list (unique first), then from category,
    then lowest limit. Disabled rules are listed too, so you can turn them back on."""
    from_category = aliased(Category)
    to_category = aliased(Category)
    rows = session.execute(
        select(
            AmountRule,
            from_category.name,
            from_category.hidden,
            to_category.name,
            to_category.hidden,
        )
        .join(from_category, from_category.id == AmountRule.from_category_id)
        .join(to_category, to_category.id == AmountRule.to_category_id)
    ).all()
    found = [
        AmountRuleRow(
            id=rule.id,
            list_key=rule.list_key,
            below_cents=rule.below_cents,
            enabled=rule.enabled,
            from_category_id=rule.from_category_id,
            from_category_name=from_name,
            from_category_hidden=from_hidden,
            to_category_id=rule.to_category_id,
            to_category_name=to_name,
            to_category_hidden=to_hidden,
        )
        for rule, from_name, from_hidden, to_name, to_hidden in rows
    ]
    return sorted(
        found,
        key=lambda row: (
            PAYEE_LISTS.index(row.list_key),
            row.from_category_name.casefold(),
            row.below_cents,
            row.id,
        ),
    )


def get_amount_rule(session: Session, rule_id: int) -> AmountRuleRow:
    """Return one amount rule with its category names. Raises LookupError if it does not exist."""
    for row in amount_rules(session):
        if row.id == rule_id:
            return row
    raise LookupError(f"There is no amount rule {rule_id}.")


def parse_dollars(text: str) -> int:
    """Dollars typed by a person, as cents. Raises ValueError with a message for the screen."""
    try:
        return to_cents((text or "").strip())
    except ValueError:
        raise ValueError("Enter an amount in dollars, such as 20.00.") from None


def keyword_list_keys(session: Session) -> list[str]:
    """Every list key that has at least one keyword, sorted."""
    return sorted(set(session.scalars(select(Keyword.list_key))))


def receipt_vendor_keys() -> list[str]:
    """The keys of the built-in and registered receipt vendors, sorted."""
    from omsync.receipts.vendors import ensure_registered  # imported here to keep ledger light

    ensure_registered()
    return vendor_registry.vendor_keys()


def uncategorized_payees(session: Session, limit: int = 100) -> list[tuple[str, int, int]]:
    """Payees whose transactions have no category: (payee_raw, count, total_cents).

    Only transactions with origin none and no splits count. The most frequent payee comes first.
    """
    count = func.count(Transaction.id)
    rows = session.execute(
        select(Transaction.payee_raw, count, func.sum(Transaction.amount_cents))
        .where(
            Transaction.category_origin == CategoryOrigin.none,
            ~Transaction.splits.any(),
        )
        .group_by(Transaction.payee_raw)
        .order_by(count.desc(), Transaction.payee_raw)
        .limit(limit)
    ).all()
    return [(payee, int(n), int(total or 0)) for payee, n, total in rows]


def suggest_pattern(payee_raw: str) -> str:
    """A conservative, escaped regex for a payee: its longest word of four or more letters.

    The word is uppercased; matching ignores case anyway. A payee with no such word falls back to
    its whole text, escaped, so a suggestion is never broader than the payee itself.
    """
    words = [word for word in _WORD.findall(payee_raw) if len(word) >= 4]
    if words:
        return re.escape(max(words, key=len).upper())
    return re.escape(payee_raw.strip())


def has_receipt_files(settings: Settings, vendor_key: str) -> bool:
    """True when the vendor's receipts folder holds at least one file the vendor can discover."""
    vendor = vendor_registry.get_vendor(vendor_key)
    return bool(vendor.discover(settings.receipts_dir / vendor_key.upper()))


def unknown_receipt_items(
    session: Session, settings: Settings, vendor_key: str, limit: int = 100
) -> list[tuple[str, int]]:
    """Receipt items that no keyword in the vendor's list matches: (text, count).

    The vendor discovers and parses every file under <data_dir>/receipts/<vendor_key>/. A file
    that cannot be read is skipped, so a bad receipt never blocks triage. Items are grouped by
    their text, ignoring case and extra spaces, and the most frequent comes first.
    """
    vendor = vendor_registry.get_vendor(vendor_key)
    keywords = load_keywords(session)
    counts: Counter[str] = Counter()
    display: dict[str, str] = {}
    for path in vendor.discover(settings.receipts_dir / vendor_key.upper()):
        try:
            receipts = vendor.parse(path)
        except (OSError, ValueError):
            continue
        for receipt in receipts:
            for item in receipt.items:
                text = " ".join(item.description.split())
                if not text or match_receipt_item(vendor_key, text, keywords) is not None:
                    continue
                key = text.casefold()
                display.setdefault(key, text)
                counts[key] += 1
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], display[pair[0]].casefold()))
    return [(display[key], count) for key, count in ranked[:limit]]


# Categories: writes


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
    transaction, split, keyword, amount rule, or any other row still points at it."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _category_snapshot(session, category_id)
    if _category_in_use(session, category_id):
        raise ValueError(IN_USE)
    return _write(
        session,
        "category.delete",
        [Change("category", str(category_id), before, None)],
        f"Deleted category {before['name']}",
        idempotency_key,
        category_id=category_id,
    )


# Keywords: writes


def add_keyword(
    session: Session,
    list_key: str,
    pattern: str,
    category_id: int,
    *,
    is_regex: bool = True,
    priority: int = 0,
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
    _check_priority(priority)
    _ensure_pattern_free(session, list_key, clean, except_id=None)
    new_id = next_primary_key(session, Keyword.__table__)
    values = {
        "id": new_id,
        "list_key": list_key,
        "pattern": clean,
        "is_regex": bool(is_regex),
        "category_id": category_id,
        "priority": priority,
        "enabled": True,
    }
    return _write(
        session,
        "keyword.add",
        [Change("keyword", str(new_id), None, values)],
        f'Added keyword "{clean}" to {list_key}',
        idempotency_key,
        keyword_id=new_id,
    )


def update_keyword(
    session: Session, keyword_id: int, *, idempotency_key: str, **fields: Any
) -> Batch:
    """Change a keyword's pattern, is_regex, category_id, or priority. Enabling is separate.

    Raises LookupError if it does not exist, ValueError for a bad value, and NothingChanged when
    the values are already stored.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    unknown = set(fields) - {"pattern", "is_regex", "category_id", "priority"}
    if unknown:
        raise ValueError(f"A keyword has no field {sorted(unknown)[0]!r} that can be changed here.")
    before = _keyword_snapshot(session, keyword_id)
    is_regex = bool(fields.get("is_regex", before["is_regex"]))
    pattern = _clean_pattern(fields.get("pattern", before["pattern"]), is_regex=is_regex)
    category_id = fields.get("category_id", before["category_id"])
    priority = fields.get("priority", before["priority"])
    _check_category(session, category_id)
    _check_priority(priority)
    after = {
        **before,
        "pattern": pattern,
        "is_regex": is_regex,
        "category_id": category_id,
        "priority": priority,
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


# Amount rules: writes


def add_amount_rule(
    session: Session,
    list_key: str,
    from_category_id: int,
    below_cents: int,
    to_category_id: int,
    *,
    idempotency_key: str,
) -> Batch:
    """Add an amount rule: a payee keyword from this list that assigns from_category goes to
    to_category when the transaction's absolute amount is below below_cents.

    Raises ValueError when the list is not unique or generic, a category is missing or both are
    the same, the limit is not positive, or the list already has a rule for that category and limit.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    _check_amount_rule(session, list_key, from_category_id, below_cents, to_category_id)
    _ensure_rule_free(session, list_key, from_category_id, below_cents, except_id=None)
    new_id = next_primary_key(session, AmountRule.__table__)
    values = {
        "id": new_id,
        "list_key": list_key,
        "from_category_id": from_category_id,
        "below_cents": below_cents,
        "to_category_id": to_category_id,
        "enabled": True,
    }
    return _write(
        session,
        "amount_rule.add",
        [Change("amount_rule", str(new_id), None, values)],
        _rule_label("Added", session, values),
        idempotency_key,
        amount_rule_id=new_id,
    )


def update_amount_rule(
    session: Session, rule_id: int, *, idempotency_key: str, **fields: Any
) -> Batch:
    """Change an amount rule's list_key, from_category_id, below_cents, or to_category_id.
    Enabling is separate.

    Raises LookupError if it does not exist, ValueError for a bad value, and NothingChanged when
    the values are already stored.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    unknown = set(fields) - {"list_key", "from_category_id", "below_cents", "to_category_id"}
    if unknown:
        raise ValueError(
            f"An amount rule has no field {sorted(unknown)[0]!r} that can be changed here."
        )
    before = _amount_rule_snapshot(session, rule_id)
    after = {**before, **fields}
    _check_amount_rule(
        session,
        after["list_key"],
        after["from_category_id"],
        after["below_cents"],
        after["to_category_id"],
    )
    if after == before:
        raise NothingChanged(NOTHING_CHANGED)
    _ensure_rule_free(
        session,
        after["list_key"],
        after["from_category_id"],
        after["below_cents"],
        except_id=rule_id,
    )
    return _write(
        session,
        "amount_rule.update",
        [Change("amount_rule", str(rule_id), before, after)],
        _rule_label("Updated", session, after),
        idempotency_key,
        amount_rule_id=rule_id,
    )


def set_amount_rule_enabled(
    session: Session, rule_id: int, enabled: bool, *, idempotency_key: str
) -> Batch:
    """Turn an amount rule on or off. A disabled rule is ignored by matching."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _amount_rule_snapshot(session, rule_id)
    after = {**before, "enabled": bool(enabled)}
    if after == before:
        raise NothingChanged(NOTHING_CHANGED)
    return _write(
        session,
        "amount_rule.enable" if enabled else "amount_rule.disable",
        [Change("amount_rule", str(rule_id), before, after)],
        _rule_label("Turned on" if enabled else "Turned off", session, before),
        idempotency_key,
        amount_rule_id=rule_id,
    )


def delete_amount_rule(session: Session, rule_id: int, *, idempotency_key: str) -> Batch:
    """Delete an amount rule. Undo restores it with its id."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _amount_rule_snapshot(session, rule_id)
    return _write(
        session,
        "amount_rule.delete",
        [Change("amount_rule", str(rule_id), before, None)],
        _rule_label("Deleted", session, before),
        idempotency_key,
        amount_rule_id=rule_id,
    )


# Helpers


def _write(
    session: Session,
    kind: str,
    changes: list[Change],
    label: str,
    idempotency_key: str,
    **summary: Any,
) -> Batch:
    preview = make_preview(session, kind, changes, summary={"label": label, **summary})
    return apply(session, preview, idempotency_key=idempotency_key)


def _category_row(row: Category) -> CategoryRow:
    return CategoryRow(
        id=row.id,
        name=row.name,
        type=CategoryType(row.type).value,
        hidden=bool(row.hidden),
        icon=row.icon or DEFAULT_CATEGORY_ICON,
    )


def _category_snapshot(session: Session, category_id: int) -> dict[str, Any]:
    before = snapshot(session, "category", category_id)
    if before is None:
        raise LookupError(f"There is no category {category_id}.")
    return before


def _keyword_snapshot(session: Session, keyword_id: int) -> dict[str, Any]:
    before = snapshot(session, "keyword", keyword_id)
    if before is None:
        raise LookupError(f"There is no keyword {keyword_id}.")
    return before


def _amount_rule_snapshot(session: Session, rule_id: int) -> dict[str, Any]:
    before = snapshot(session, "amount_rule", rule_id)
    if before is None:
        raise LookupError(f"There is no amount rule {rule_id}.")
    return before


def _clean_name(name: str) -> str:
    clean = (name or "").strip()
    if not clean:
        raise ValueError("Give the category a name.")
    return clean


def _category_type(value: CategoryType | str) -> CategoryType:
    try:
        return CategoryType(value)
    except ValueError:
        raise ValueError("Pick a type for the category.") from None


def _icon(value: str | None) -> str | None:
    """The stored icon name, or None for the default. Whether Lucide has the icon is the web
    layer's check, because the icon files are its assets."""
    clean = (value or "").strip()
    if not clean or clean == DEFAULT_CATEGORY_ICON:
        return None
    if len(clean) > 64 or not _ICON_NAME.fullmatch(clean):
        raise ValueError("Pick an icon from the list.")
    return clean


def _ensure_name_free(session: Session, name: str, *, except_id: int | None) -> None:
    for row_id, existing in session.execute(select(Category.id, Category.name)).all():
        if row_id != except_id and existing.casefold() == name.casefold():
            raise ValueError(f"There is already a category named {existing}.")


def _category_in_use(session: Session, category_id: int) -> bool:
    """True when any table with a foreign key to category holds a row that points at it."""
    category_table = Category.__table__
    for table in Base.metadata.tables.values():
        for foreign_key in table.foreign_keys:
            target = foreign_key.column
            if target.table is not category_table or target.name != "id":
                continue
            column = table.c[foreign_key.parent.name]
            if session.scalar(select(column).where(column == category_id).limit(1)) is not None:
                return True
    return False


def _check_list_key(list_key: str) -> None:
    if is_list_key(list_key):
        return
    raise ValueError("The list must be unique, generic, or a lowercase vendor key such as amazon.")


def _clean_pattern(pattern: str, *, is_regex: bool) -> str:
    """The stored pattern: stripped, not blank, and a valid regex when is_regex is set.

    The error never repeats the pattern, because a pattern can be personal.
    """
    clean = (pattern or "").strip()
    if not clean:
        raise ValueError("Enter a pattern.")
    if is_regex:
        try:
            compile_pattern(clean, is_regex=True)
        except re.error as exc:
            raise ValueError(f"Not a valid regular expression: {exc.msg}.") from None
    return clean


def _check_category(session: Session, category_id: int, message: str = "Pick a category.") -> None:
    if not isinstance(category_id, int) or session.get(Category, category_id) is None:
        raise ValueError(message)


def _check_priority(priority: int) -> None:
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ValueError("Priority must be a whole number.")


def _ensure_pattern_free(
    session: Session, list_key: str, pattern: str, *, except_id: int | None
) -> None:
    rows = session.execute(
        select(Keyword.id, Keyword.pattern).where(Keyword.list_key == list_key)
    ).all()
    for row_id, existing in rows:
        if row_id != except_id and existing.casefold() == pattern.casefold():
            raise ValueError("This list already has that pattern.")


def _check_amount_rule(
    session: Session, list_key: str, from_category_id: int, below_cents: int, to_category_id: int
) -> None:
    if list_key not in PAYEE_LISTS:
        raise ValueError("The list must be unique or generic.")
    _check_category(session, from_category_id, "Pick the category to change from.")
    _check_category(session, to_category_id, "Pick the category to change to.")
    if from_category_id == to_category_id:
        raise ValueError("Pick two different categories.")
    if isinstance(below_cents, bool) or not isinstance(below_cents, int) or below_cents <= 0:
        raise ValueError("The limit must be more than $0.00.")


def _ensure_rule_free(
    session: Session,
    list_key: str,
    from_category_id: int,
    below_cents: int,
    *,
    except_id: int | None,
) -> None:
    found = session.scalars(
        select(AmountRule.id).where(
            AmountRule.list_key == list_key,
            AmountRule.from_category_id == from_category_id,
            AmountRule.below_cents == below_cents,
        )
    ).all()
    if any(row_id != except_id for row_id in found):
        raise ValueError("This list already has a rule for that category and limit.")


def _rule_label(verb: str, session: Session, values: dict[str, Any]) -> str:
    names = dict(
        session.execute(
            select(Category.id, Category.name).where(
                Category.id.in_([values["from_category_id"], values["to_category_id"]])
            )
        ).all()
    )
    return (
        f"{verb} amount rule: {values['list_key']} {names[values['from_category_id']]} "
        f"under {format_cents(values['below_cents'])} to {names[values['to_category_id']]}"
    )
