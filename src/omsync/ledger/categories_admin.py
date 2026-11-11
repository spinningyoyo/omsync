"""Admin writes for categories, keywords, and rules, and the reads the Categories page needs.

Every write is one batch from omsync.core.batches, with an idempotency key. A key that already
applied returns its batch, even if the data has moved on, so a retried form changes nothing twice.
A refused change raises ValueError with a message for the screen, and nothing is written.

Triage reads receipt files through the vendor registry. This module never imports omsync.web.
"""

import copy
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import func, or_, select
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
    snapshot,
)
from omsync.core.db import Base
from omsync.core.money import format_cents, to_cents
from omsync.core.settings import Settings
from omsync.ledger.categories import DEFAULT_CATEGORY_ICON
from omsync.ledger.categorize import (
    ActiveRule,
    CompiledKeyword,
    Move,
    compile_pattern,
    is_list_key,
    list_name,
    load_keywords,
    match_receipt_item,
    plan_categorize,
    rule_for_transaction,
    with_keyword,
)
from omsync.ledger.models import (
    Account,
    BudgetLine,
    Category,
    CategoryOrigin,
    CategoryRule,
    CategoryType,
    Contact,
    ContactRole,
    Keyword,
    RuleCounterparty,
    SavedFilter,
    Transaction,
)
from omsync.ledger.payment_methods import (
    PaymentMethod,
    describe_methods,
    parse_paid_by,
    store_paid_by,
)
from omsync.receipts import vendor as vendor_registry
from omsync.reports.rental import RENTAL_CATEGORIES, get_rental_categories

NOTHING_CHANGED = "Nothing has changed."
IN_USE = "in use: hide it instead"
STALE = "This changed elsewhere. Preview again."
SAVED_FILTER_USE = "saved_filter"
RENTAL_NAME_USE = "rental_name"
# A kind of use is a table name, or one of the two kinds above. Each has a singular and a plural.
_USE_NAMES = {
    "transaction": ("transaction", "transactions"),
    "split": ("split line", "split lines"),
    "keyword": ("keyword", "keywords"),
    "category_rule": ("rule", "rules"),
    "budget_line": ("budget line", "budget lines"),
    "recurring_series": ("recurring series", "recurring series"),
    SAVED_FILTER_USE: ("saved filter", "saved filters"),
    RENTAL_NAME_USE: ("rental category name", "rental category names"),
}
# Tables whose category columns a move rewrites by its own rules, not by repointing every row.
_BY_HAND = frozenset({"category_rule", "budget_line"})
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
    in_use: bool  # something points at it, so it deletes only by moving its uses


@dataclass(frozen=True)
class CategoryUse:
    """How many rows of one kind point at a category. The kind is a table name, or one of the
    kinds for saved filters and rental category names."""

    kind: str
    count: int

    @property
    def text(self) -> str:
        singular, plural = _USE_NAMES.get(self.kind, (self.kind, self.kind))
        return f"{self.count} {singular if self.count == 1 else plural}"


@dataclass(frozen=True)
class KeywordRow:
    id: int
    list_key: str
    pattern: str
    is_regex: bool
    enabled: bool
    category_id: int
    category_name: str
    category_hidden: bool


@dataclass(frozen=True)
class RuleRow:
    """One rule with the names its conditions show, and the sentence that says what it does.
    Rules are listed in position order: the first that holds sets its category."""

    id: int
    position: int
    enabled: bool
    paid_by: tuple[str, ...]  # PaymentMethod values, in the order PaymentMethod lists them
    below_cents: int | None
    at_least_cents: int | None
    counterparty: str | None  # the RuleCounterparty value
    contact_id: int | None
    contact_name: str | None
    account_id: int | None
    account_name: str | None
    to_category_id: int
    to_category_name: str
    to_category_hidden: bool
    sentence: str


@dataclass(frozen=True)
class RuleDraft:
    """A rule as it would be stored: its conditions and the category it sets. Nothing is stored
    until add_rule or update_rule, and preview_rule checks a draft the same way and writes nothing.
    counterparty is a RuleCounterparty, or its value, and paid_by holds PaymentMethods or their
    values."""

    to_category_id: int
    paid_by: Iterable[PaymentMethod | str] = ()
    below_cents: int | None = None
    at_least_cents: int | None = None
    counterparty: RuleCounterparty | str | None = None
    contact_id: int | None = None
    account_id: int | None = None


@dataclass(frozen=True)
class DraftPreview:
    """The transactions whose category a draft would change, were it saved now. count is their
    number, and changes lists them in ID order."""

    count: int
    changes: tuple[Move, ...]


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
    usage = _category_usage(session)
    rows = [_category_row(row, row.id in usage) for row in session.scalars(query)]
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
    return _category_row(row, category_id in _category_usage(session))


def category_uses(session: Session, category_id: int) -> list[CategoryUse]:
    """What points at a category, by kind, in the order the dialog lists them. An empty list means
    nothing uses it."""
    counts = _category_usage(session).get(category_id, Counter())
    order = tuple(_USE_NAMES)
    ordered = sorted(
        counts.items(),
        key=lambda item: (order.index(item[0]) if item[0] in order else len(order), item[0]),
    )
    return [CategoryUse(kind, count) for kind, count in ordered]


def category_names(session: Session) -> dict[int, str]:
    """Every category's name by id, hidden ones included, for naming what a keyword points at."""
    return dict(session.execute(select(Category.id, Category.name)).all())


def category_choices(session: Session) -> list[tuple[int, str]]:
    """(id, name) for the categories a keyword may point at: not hidden, sorted by name."""
    rows = session.execute(
        select(Category.id, Category.name).where(Category.hidden.is_(False))
    ).all()
    return sorted(((int(cid), name) for cid, name in rows), key=lambda row: row[1].casefold())


def keywords_in_list(session: Session, list_key: str, query: str = "") -> list[KeywordRow]:
    """The keywords of one list, in precedence order: longer pattern first, then id.

    Disabled keywords are listed too, at their precedence, so you can turn them back on. A query
    keeps the keywords whose pattern or category name contains it, ignoring case. A blank query
    keeps them all.
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
            enabled=keyword.enabled,
            category_id=keyword.category_id,
            category_name=name,
            category_hidden=hidden,
        )
        for keyword, name, hidden in rows
    ]
    needle = query.strip().casefold()
    if needle:
        found = [
            row
            for row in found
            if needle in row.pattern.casefold() or needle in row.category_name.casefold()
        ]
    return sorted(found, key=lambda row: (-len(row.pattern), row.id))


def keyword_count(session: Session, list_key: str) -> int:
    """How many keywords the list has, disabled ones included."""
    return int(
        session.scalar(
            select(func.count()).select_from(Keyword).where(Keyword.list_key == list_key)
        )
        or 0
    )


def get_keyword(session: Session, keyword_id: int) -> KeywordRow:
    """Return one keyword with its category. Raises LookupError if it does not exist."""
    keyword = session.get(Keyword, keyword_id)
    if keyword is None:
        raise LookupError(f"There is no keyword {keyword_id}.")
    for row in keywords_in_list(session, keyword.list_key):
        if row.id == keyword_id:
            return row
    raise LookupError(f"There is no keyword {keyword_id}.")  # pragma: no cover - same query


# The conditions a rule has, in the order a draft names them, and the fields a rule stores.
_CONDITIONS = (
    "paid_by",
    "below_cents",
    "at_least_cents",
    "counterparty",
    "contact_id",
    "account_id",
)
_RULE_FIELDS = (*_CONDITIONS, "to_category_id")


@dataclass(frozen=True)
class _Names:
    """The names a rule's sentence shows, loaded once for a page or a label."""

    categories: dict[int, str]
    hidden: frozenset[int]
    contacts: dict[int, str]
    accounts: dict[int, str]


# Reads


def rule_rows(session: Session) -> list[RuleRow]:
    """Every rule in position order, with the sentence that says what it does. Disabled rules are
    listed too, so you can turn them back on."""
    names = _names(session)
    rules = session.scalars(select(CategoryRule).order_by(CategoryRule.position, CategoryRule.id))
    return [_row(rule, names) for rule in rules]


def get_rule(session: Session, rule_id: int) -> RuleRow:
    """Return one rule with its names. Raises LookupError if it does not exist."""
    rule = session.get(CategoryRule, rule_id)
    if rule is None:
        raise LookupError(f"There is no rule {rule_id}.")
    return _row(rule, _names(session))


def transaction_rule(session: Session, transaction_id: int) -> RuleRow | None:
    """The rule that holds for a transaction now, with its sentence, or None when none does. The
    transaction's origin says whether a rule set its category; this says which rule holds for it."""
    rule = rule_for_transaction(session, transaction_id)
    return None if rule is None else get_rule(session, rule.id)


def contact_choices(session: Session) -> list[tuple[int, str]]:
    """(id, name) for the contacts a rule may name as the counterparty: everyone but you."""
    rows = session.execute(
        select(Contact.id, Contact.name).where(Contact.role != ContactRole.owner)
    ).all()
    return sorted(((int(cid), name) for cid, name in rows), key=lambda row: row[1].casefold())


def account_choices(session: Session) -> list[tuple[int, str]]:
    """(id, name) for the tracked accounts a rule may name, by name."""
    rows = session.execute(select(Account.id, Account.name)).all()
    return sorted(((int(aid), name) for aid, name in rows), key=lambda row: row[1].casefold())


def _names(session: Session) -> _Names:
    categories = session.execute(select(Category.id, Category.name, Category.hidden)).all()
    return _Names(
        categories={row.id: row.name for row in categories},
        hidden=frozenset(row.id for row in categories if row.hidden),
        contacts=dict(session.execute(select(Contact.id, Contact.name)).all()),
        accounts=dict(session.execute(select(Account.id, Account.name)).all()),
    )


def _row(rule: CategoryRule, names: _Names) -> RuleRow:
    fields = _fields_of(rule)
    to_id = fields["to_category_id"]
    return RuleRow(
        id=rule.id,
        position=rule.position,
        enabled=rule.enabled,
        paid_by=tuple(m.value for m in PaymentMethod if m in parse_paid_by(fields["paid_by"])),
        below_cents=fields["below_cents"],
        at_least_cents=fields["at_least_cents"],
        counterparty=fields["counterparty"],
        contact_id=fields["contact_id"],
        contact_name=names.contacts.get(fields["contact_id"]),
        account_id=fields["account_id"],
        account_name=names.accounts.get(fields["account_id"]),
        to_category_id=to_id,
        to_category_name=names.categories[to_id],
        to_category_hidden=to_id in names.hidden,
        sentence=_sentence(names, **fields),
    )


def _fields_of(rule: CategoryRule) -> dict[str, Any]:
    """A stored rule's values, named as a draft names them, with the counterparty as its value."""
    other = None if rule.counterparty is None else RuleCounterparty(rule.counterparty).value
    return {
        "paid_by": rule.paid_by,
        "below_cents": rule.below_cents,
        "at_least_cents": rule.at_least_cents,
        "counterparty": other,
        "contact_id": rule.contact_id,
        "account_id": rule.account_id,
        "to_category_id": rule.to_category_id,
    }


def _sentence(
    names: _Names,
    *,
    paid_by: str | None,
    below_cents: int | None,
    at_least_cents: int | None,
    counterparty: str | None,
    contact_id: int | None,
    account_id: int | None,
    to_category_id: int,
) -> str:
    """A rule as one sentence, such as "Paid by Transfer · under $100.00 · with a person →
    Dining"."""
    parts = [f"Paid by {describe_methods(parse_paid_by(paid_by))}"] if paid_by else []
    amount = _amount_text(below_cents, at_least_cents)
    if amount is not None:
        parts.append(amount)
    if counterparty == RuleCounterparty.person:
        parts.append("with a person")
    elif counterparty == RuleCounterparty.contact:
        parts.append(f"with {names.contacts[contact_id]}")
    elif counterparty == RuleCounterparty.owner_account:
        parts.append("with one of my accounts")
    if account_id is not None:
        parts.append(f"in {names.accounts[account_id]}")
    joined = " · ".join(parts) or "Any transaction"
    return f"{joined} → {names.categories[to_category_id]}"


def _amount_text(below_cents: int | None, at_least_cents: int | None) -> str | None:
    if below_cents is not None and at_least_cents is not None:
        return f"{format_cents(at_least_cents)} or more and under {format_cents(below_cents)}"
    if below_cents is not None:
        return f"under {format_cents(below_cents)}"
    if at_least_cents is not None:
        return f"{format_cents(at_least_cents)} or more"
    return None


def _sentence_for(session: Session, fields: Mapping[str, Any]) -> str:
    """The sentence for a rule's values, as a batch label names it."""
    return _sentence(_names(session), **{name: fields[name] for name in _RULE_FIELDS})


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
    transaction, split, keyword, rule, saved filter, rental name, or any other row still
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
    target already has a budget line for a month the category has one.
    """
    source = _category_snapshot(session, category_id)
    if target_id == category_id:
        raise ValueError("Pick a different category to move its uses to.")
    target = session.get(Category, target_id)
    if target is None:
        raise ValueError("Pick a category to move its uses to.")
    source_name = str(source["name"])
    changes = [
        *_rule_changes(session, category_id, target_id),
        *_budget_line_changes(session, category_id, target_id, source_name, target.name),
        *_repoint_changes(session, category_id, target_id),
        *_saved_filter_changes(session, category_id, target_id),
        *_rental_name_changes(session, source_name, target.name),
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
    reverses. Keywords move as they are, transactions and splits keep their origin, and rental
    category names become the target's name. See move_preview for what is refused.

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


# Keywords: writes


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
        raise ValueError(f"A keyword has no field {sorted(unknown)[0]!r} that can be changed here.")
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


# Rules: writes


def add_rule(session: Session, draft: RuleDraft, *, idempotency_key: str) -> Batch:
    """Add a rule at the end of the order, so it is tried after every rule already there. Raises
    ValueError with a message for the screen when the draft is refused, and nothing is written."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    values = _check_rule(session, draft)
    _ensure_rule_free(session, values, except_id=None)
    new_id = next_primary_key(session, CategoryRule.__table__)
    last = session.scalar(select(func.max(CategoryRule.position)))
    row = {"id": new_id, "position": (last or 0) + 1, "enabled": True, **values}
    return _write(
        session,
        "rule.add",
        [Change("category_rule", str(new_id), None, row)],
        f"Added rule: {_sentence_for(session, values)}",
        idempotency_key,
        rule_id=new_id,
    )


def update_rule(session: Session, rule_id: int, draft: RuleDraft, *, idempotency_key: str) -> Batch:
    """Replace a rule's conditions and category. Its place in the order and whether it is on stay as
    they are. Raises LookupError if it does not exist, ValueError for a refused draft, and
    NothingChanged when the values are already stored."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _rule_snapshot(session, rule_id)
    values = _check_rule(session, draft)
    after = {**before, **values}
    if after == before:
        raise NothingChanged(NOTHING_CHANGED)
    _ensure_rule_free(session, values, except_id=rule_id)
    return _write(
        session,
        "rule.update",
        [Change("category_rule", str(rule_id), before, after)],
        f"Updated rule: {_sentence_for(session, values)}",
        idempotency_key,
        rule_id=rule_id,
    )


def set_rule_enabled(
    session: Session, rule_id: int, enabled: bool, *, idempotency_key: str
) -> Batch:
    """Turn a rule on or off. A disabled rule is skipped by categorize."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _rule_snapshot(session, rule_id)
    after = {**before, "enabled": bool(enabled)}
    if after == before:
        raise NothingChanged(NOTHING_CHANGED)
    verb = "Turned on" if enabled else "Turned off"
    return _write(
        session,
        "rule.enable" if enabled else "rule.disable",
        [Change("category_rule", str(rule_id), before, after)],
        f"{verb} rule: {_sentence_for(session, before)}",
        idempotency_key,
        rule_id=rule_id,
    )


def delete_rule(session: Session, rule_id: int, *, idempotency_key: str) -> Batch:
    """Delete a rule. Undo restores it with its id. Transactions it set keep its origin until the
    next categorize, which sets them again or leaves them unset."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _rule_snapshot(session, rule_id)
    return _write(
        session,
        "rule.delete",
        [Change("category_rule", str(rule_id), before, None)],
        f"Deleted rule: {_sentence_for(session, before)}",
        idempotency_key,
        rule_id=rule_id,
    )


def move_rule(
    session: Session,
    rule_id: int,
    direction: Literal["up", "down"],
    *,
    idempotency_key: str,
) -> Batch:
    """Move a rule one place earlier (up) or later (down), by swapping places with the rule beside
    it, enabled or not, as one batch. Raises LookupError if the rule does not exist, ValueError for
    a direction other than up or down, and NothingChanged at the first or last place."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    if direction not in ("up", "down"):
        raise ValueError("Move a rule up or down.")
    ordered = list(
        session.scalars(select(CategoryRule.id).order_by(CategoryRule.position, CategoryRule.id))
    )
    if rule_id not in ordered:
        raise LookupError(f"There is no rule {rule_id}.")
    index = ordered.index(rule_id)
    other_index = index - 1 if direction == "up" else index + 1
    if not 0 <= other_index < len(ordered):
        raise NothingChanged("It is already first." if direction == "up" else "It is already last.")
    mover = _rule_snapshot(session, rule_id)
    other = _rule_snapshot(session, ordered[other_index])
    if mover["position"] == other["position"]:
        raise NothingChanged(NOTHING_CHANGED)
    # Two rows change, each once, so undo can restore each from its own snapshot.
    changes = [
        Change("category_rule", str(rule_id), mover, {**mover, "position": other["position"]}),
        Change("category_rule", str(other["id"]), other, {**other, "position": mover["position"]}),
    ]
    return _write(
        session,
        "rule.move",
        changes,
        f"Moved rule {direction}: {_sentence_for(session, mover)}",
        idempotency_key,
        rule_id=rule_id,
    )


def preview_rule(session: Session, draft: RuleDraft, *, rule_id: int | None = None) -> DraftPreview:
    """The transactions whose category would change if the draft were saved now, with their count.

    A draft with rule_id replaces that rule, keeping its place and whether it is on. Without
    rule_id, the draft would be added last. Writes nothing. Raises ValueError for a draft that
    add_rule or update_rule would refuse, and LookupError if rule_id does not exist.
    """
    values = _check_rule(session, draft)
    stored = list(
        session.scalars(select(CategoryRule).order_by(CategoryRule.position, CategoryRule.id))
    )
    if rule_id is not None and all(rule.id != rule_id for rule in stored):
        raise LookupError(f"There is no rule {rule_id}.")
    rules: list[ActiveRule] = []
    for rule in stored:
        if rule.id == rule_id:
            if rule.enabled:
                rules.append(_active(rule.id, rule.position, values))
        elif rule.enabled:
            rules.append(_active(rule.id, rule.position, _fields_of(rule)))
    if rule_id is None:
        rules.append(_active(0, (stored[-1].position + 1) if stored else 1, values))
    plan = plan_categorize(session, rules=rules)
    return DraftPreview(count=plan.changed, changes=plan.moves)


def preview_keyword(
    session: Session,
    list_key: str,
    pattern: str,
    category_id: int,
    *,
    is_regex: bool = True,
    keyword_id: int | None = None,
) -> DraftPreview:
    """The transactions whose category would change if the keyword were saved now, with their count.

    A draft with keyword_id replaces that keyword, which stays out if it is switched off. Without
    keyword_id, the draft would be added. Writes nothing. Raises ValueError for a draft that
    add_keyword or update_keyword would refuse, and LookupError if keyword_id does not exist. A
    receipt list's keyword changes no transaction here: it categorizes receipt items, which change
    when a vendor's receipts are applied.
    """
    _check_list_key(list_key)
    clean = _clean_pattern(pattern, is_regex=is_regex)
    _check_category(session, category_id)
    enabled = True
    if keyword_id is not None:
        stored = session.get(Keyword, keyword_id)
        if stored is None:
            raise LookupError(f"There is no keyword {keyword_id}.")
        list_key, enabled = stored.list_key, stored.enabled
    _ensure_pattern_free(session, list_key, clean, except_id=keyword_id)
    if keyword_id is None:
        keyword_id_for_draft = next_primary_key(session, Keyword.__table__)
    else:
        keyword_id_for_draft = keyword_id
    draft = CompiledKeyword(
        keyword_id_for_draft,
        list_key,
        clean,
        category_id,
        compile_pattern(clean, is_regex=is_regex),
    )
    keywords = with_keyword(
        load_keywords(session), draft if enabled else None, replacing=keyword_id
    )
    plan = plan_categorize(session, keywords=keywords)
    return DraftPreview(count=plan.changed, changes=plan.moves)


def _active(rule_id: int, position: int, fields: Mapping[str, Any]) -> ActiveRule:
    other = fields["counterparty"]
    return ActiveRule(
        id=rule_id,
        position=position,
        paid_by=parse_paid_by(fields["paid_by"]),
        below_cents=fields["below_cents"],
        at_least_cents=fields["at_least_cents"],
        counterparty=None if other is None else RuleCounterparty(other),
        contact_id=fields["contact_id"],
        account_id=fields["account_id"],
        to_category_id=fields["to_category_id"],
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


def _category_row(row: Category, in_use: bool) -> CategoryRow:
    return CategoryRow(
        id=row.id,
        name=row.name,
        type=CategoryType(row.type).value,
        hidden=bool(row.hidden),
        icon=row.icon or DEFAULT_CATEGORY_ICON,
        in_use=in_use,
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


def _rule_snapshot(session: Session, rule_id: int) -> dict[str, Any]:
    before = snapshot(session, "category_rule", rule_id)
    if before is None:
        raise LookupError(f"There is no rule {rule_id}.")
    return before


def _condition_key(fields: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(fields[name] for name in _CONDITIONS)


def _check_rule(session: Session, draft: RuleDraft) -> dict[str, Any]:
    """The values a draft stores, checked. Raises ValueError with a message for the screen.

    A rule needs at least one condition. A limit or minimum must be more than $0.00, and the under
    limit must be more than the minimum. A contact goes with the contact option and no other.
    """
    methods = _methods(draft.paid_by)
    values: dict[str, Any] = {
        "paid_by": store_paid_by(methods),
        "below_cents": draft.below_cents,
        "at_least_cents": draft.at_least_cents,
        "counterparty": None if draft.counterparty is None else _counterparty(draft.counterparty),
        "contact_id": draft.contact_id,
        "account_id": draft.account_id,
        "to_category_id": draft.to_category_id,
    }
    _check_category(session, draft.to_category_id, "Pick the category to set.")
    if draft.below_cents is not None:
        _check_money(draft.below_cents, "The limit must be more than $0.00.")
    if draft.at_least_cents is not None:
        _check_money(draft.at_least_cents, "The minimum must be more than $0.00.")
    if draft.below_cents is not None and draft.at_least_cents is not None:
        if draft.below_cents <= draft.at_least_cents:
            raise ValueError("The limit must be more than the minimum.")
    if values["counterparty"] == RuleCounterparty.contact:
        if draft.contact_id is None or session.get(Contact, draft.contact_id) is None:
            raise ValueError("Pick the contact.")
    elif draft.contact_id is not None:
        raise ValueError("A contact goes only with the contact option.")
    if draft.account_id is not None and session.get(Account, draft.account_id) is None:
        raise ValueError("Pick an account.")
    if all(values[name] is None for name in _CONDITIONS):
        raise ValueError("Set at least one condition.")
    return values


def _methods(values: Iterable[PaymentMethod | str]) -> frozenset[PaymentMethod]:
    try:
        return frozenset(PaymentMethod(value) for value in values)
    except ValueError:
        raise ValueError("Pick how it was paid from the list.") from None


def _counterparty(value: RuleCounterparty | str) -> str:
    try:
        return RuleCounterparty(value).value
    except ValueError:
        raise ValueError("Pick the counterparty from the list.") from None


def _check_money(cents: int, message: str) -> None:
    if isinstance(cents, bool) or not isinstance(cents, int) or cents <= 0:
        raise ValueError(message)


def _ensure_rule_free(
    session: Session, values: Mapping[str, Any], *, except_id: int | None
) -> None:
    """Refuse a rule whose conditions another rule already has, since one of them would never
    apply."""
    wanted = _condition_key(values)
    for rule in session.scalars(select(CategoryRule)):
        if rule.id != except_id and _condition_key(_fields_of(rule)) == wanted:
            raise ValueError("A rule with these conditions already exists.")


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


def _category_columns() -> dict[str, list[Any]]:
    """Every table with a foreign key to category.id, by table name, with its category columns."""
    found: dict[str, list[Any]] = {}
    for table in Base.metadata.tables.values():
        for foreign_key in table.foreign_keys:
            target = foreign_key.column
            if target.table is Category.__table__ and target.name == "id":
                found.setdefault(table.name, []).append(table.c[foreign_key.parent.name])
    return found


def _category_usage(session: Session) -> dict[int, Counter[str]]:
    """What points at each category, by kind: rows with a foreign key to it, saved filters that name
    it, and rental category names that match it. A category nothing points at is absent.

    Each table names a category in one column, so counting each column counts each row once.
    """
    usage: dict[int, Counter[str]] = {}

    def tally(category_id: int, kind: str, count: int = 1) -> None:
        usage.setdefault(category_id, Counter())[kind] += count

    for table_name, columns in _category_columns().items():
        for column in columns:
            counted = session.execute(
                select(column, func.count()).where(column.is_not(None)).group_by(column)
            ).all()
            for category_id, count in counted:
                tally(int(category_id), table_name, int(count))
    for saved in session.scalars(select(SavedFilter)):
        for category_id in _filter_category_ids(saved.conditions):
            tally(category_id, SAVED_FILTER_USE)
    ids_by_name = dict(session.execute(select(Category.name, Category.id)).all())
    for name in {name.strip() for name in get_rental_categories(session)}:
        if name in ids_by_name:
            tally(int(ids_by_name[name]), RENTAL_NAME_USE)
    return usage


def _category_conditions(data: Any) -> list[dict[str, Any]]:
    """The conditions in a saved filter's stored JSON that test the category by "is" or "is not"."""
    if not isinstance(data, dict) or not isinstance(data.get("conditions"), list):
        return []
    return [
        item
        for item in data["conditions"]
        if isinstance(item, dict)
        and item.get("field") == "category"
        and item.get("op") in ("is", "is_not")
        and isinstance(item.get("value"), list)
    ]


def _filter_category_ids(data: Any) -> set[int]:
    """The category ids a saved filter names."""
    found: set[int] = set()
    for item in _category_conditions(data):
        found.update(v for v in item["value"] if isinstance(v, int) and not isinstance(v, bool))
    return found


def _repoint_filter(data: Any, source_id: int, target_id: int) -> dict[str, Any] | None:
    """A copy of a saved filter's stored JSON with the source category replaced by the target, or
    None when the filter does not name the source."""
    repointed = copy.deepcopy(data)
    changed = False
    for item in _category_conditions(repointed):
        if source_id in item["value"]:
            item["value"] = list(
                dict.fromkeys(target_id if v == source_id else v for v in item["value"])
            )
            changed = True
    return repointed if changed else None


def _rule_changes(session: Session, source_id: int, target_id: int) -> list[Change]:
    """The rules a move changes: those that set the source now set the target."""
    rules = session.scalars(
        select(CategoryRule)
        .where(CategoryRule.to_category_id == source_id)
        .order_by(CategoryRule.id)
    )
    return [
        Change(
            "category_rule",
            str(rule.id),
            {"to_category_id": source_id},
            {"to_category_id": target_id},
        )
        for rule in rules
    ]


def _budget_line_changes(
    session: Session, source_id: int, target_id: int, source_name: str, target_name: str
) -> list[Change]:
    """The budget lines that move to the target. A month the target already budgets is refused, not
    merged: the amounts stay as they are, and the person decides which line to keep."""
    lines = list(
        session.scalars(
            select(BudgetLine).where(BudgetLine.category_id == source_id).order_by(BudgetLine.month)
        )
    )
    taken = set(
        session.scalars(select(BudgetLine.month).where(BudgetLine.category_id == target_id)).all()
    )
    clashes = [line.month for line in lines if line.month in taken]
    if clashes:
        periods = ", ".join(month.strftime("%B %Y") for month in clashes)
        raise ValueError(
            f"{target_name} already has a budget line for {periods}, so {source_name}'s line for "
            "that month cannot move. Change or delete one of them first."
        )
    return [
        Change("budget_line", str(line.id), {"category_id": source_id}, {"category_id": target_id})
        for line in lines
    ]


def _repoint_changes(session: Session, source_id: int, target_id: int) -> list[Change]:
    """Every row with a foreign key to the source points at the target instead. Only the category
    column changes, so a transaction's or split's origin stays as it was."""
    changes: list[Change] = []
    for table_name, columns in _category_columns().items():
        if table_name in _BY_HAND:
            continue
        table = Base.metadata.tables[table_name]
        key = next(iter(table.primary_key.columns))
        found = session.execute(
            select(key, *columns).where(or_(*(column == source_id for column in columns)))
        ).all()
        for row in found:
            mapping = row._mapping
            before = {
                column.name: source_id for column in columns if mapping[column.name] == source_id
            }
            after = {name: target_id for name in before}
            changes.append(Change(table_name, str(mapping[key.name]), before, after))
    return changes


def _saved_filter_changes(session: Session, source_id: int, target_id: int) -> list[Change]:
    changes: list[Change] = []
    for saved in session.scalars(select(SavedFilter).order_by(SavedFilter.id)):
        repointed = _repoint_filter(saved.conditions, source_id, target_id)
        if repointed is None:
            continue
        before = snapshot(session, "saved_filter", saved.id)
        changes.append(
            Change(
                "saved_filter", str(saved.id), before, {**(before or {}), "conditions": repointed}
            )
        )
    return changes


def _rental_name_changes(session: Session, source_name: str, target_name: str) -> list[Change]:
    """The rental category names that name the source now name the target, once each. The preference
    is a batch change, so undo puts the names back. The defaults apply until it is saved."""
    names = get_rental_categories(session)
    if not any(name.strip() == source_name for name in names):
        return []
    renamed = [target_name if name.strip() == source_name else name.strip() for name in names]
    value = list(dict.fromkeys(renamed))
    before = snapshot(session, "preference", RENTAL_CATEGORIES)
    after = (
        {"key": RENTAL_CATEGORIES, "value": value} if before is None else {**before, "value": value}
    )
    return [Change("preference", RENTAL_CATEGORIES, before, after)]


def _check_list_key(list_key: str) -> None:
    if is_list_key(list_key):
        return
    raise ValueError("The list must be payee or a lowercase vendor key such as amazon.")


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


def _ensure_pattern_free(
    session: Session, list_key: str, pattern: str, *, except_id: int | None
) -> None:
    rows = session.execute(
        select(Keyword.id, Keyword.pattern).where(Keyword.list_key == list_key)
    ).all()
    for row_id, existing in rows:
        if row_id != except_id and existing.casefold() == pattern.casefold():
            raise ValueError("This list already has that pattern.")
