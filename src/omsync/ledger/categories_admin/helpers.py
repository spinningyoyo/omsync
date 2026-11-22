"""Checks, snapshots, and repointing that the category writes share."""

import copy
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from sqlalchemy import Table, UniqueConstraint, and_, func, or_, select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    Change,
    apply,
    make_preview,
    snapshot,
)
from omsync.core.db import Base
from omsync.ledger.categories import DEFAULT_CATEGORY_ICON
from omsync.ledger.categories_admin.rows import (
    _BY_HAND,
    _ICON_NAME,
    COSTCO_FOOD_USE,
    SAVED_FILTER_USE,
    CategoryRow,
    RuleDraft,
)
from omsync.ledger.categorize import (
    compile_pattern,
    is_list_key,
)
from omsync.ledger.models import (
    Account,
    Category,
    CategoryRule,
    CategoryType,
    Contact,
    Keyword,
    RuleCounterparty,
    SavedFilter,
)
from omsync.ledger.payment_methods import (
    PaymentMethod,
    store_paid_by,
)
from omsync.ledger.preferences import get_pref
from omsync.ledger.receipt_vendors import COSTCO_FOOD_CATEGORY


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
    if (
        draft.below_cents is not None
        and draft.at_least_cents is not None
        and draft.below_cents <= draft.at_least_cents
    ):
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
    it, and the Costco food setting. A category nothing points at is absent. Its rental mark is its
    own, so it is not a use.

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
    food = get_pref(session, COSTCO_FOOD_CATEGORY)
    if isinstance(food, int) and not isinstance(food, bool):
        tally(food, COSTCO_FOOD_USE)
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


def _unique_column_sets(table: Table, columns: list[Any]) -> list[list[Any]]:
    """The column lists of the table's unique constraints and unique indexes that include a category
    column. A partial index never holds a category column here, so a kind of row it guards is
    skipped."""
    found: list[list[Any]] = [
        list(constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    ]
    found.extend(
        list(index.columns)
        for index in table.indexes
        if index.unique and index.dialect_options["sqlite"]["where"] is None
    )
    return [cols for cols in found if any(column in cols for column in columns)]


def _clash_text(value: Any) -> str:
    if isinstance(value, date):
        return value.strftime("%B %Y")
    return str(getattr(value, "value", value)).replace("_", " ")


def _check_no_clash(session: Session, source_id: int, target_id: int) -> None:
    """Refuse a move that would break a unique constraint: a row of the source whose other
    constraint columns match a row the target already has. The rows stay as they are, and the person
    decides which to keep."""
    source_name = session.scalar(select(Category.name).where(Category.id == source_id))
    target_name = session.scalar(select(Category.name).where(Category.id == target_id))
    for table_name, columns in _category_columns().items():
        table = Base.metadata.tables[table_name]
        for unique in _unique_column_sets(table, columns):
            category = next(column for column in unique if column in columns)
            others = [column for column in unique if column is not category]
            mine, theirs = table.alias("mine"), table.alias("theirs")
            query = (
                select(*(mine.c[column.name] for column in others))
                .join(
                    theirs,
                    and_(
                        theirs.c[category.name] == target_id,
                        *(mine.c[column.name] == theirs.c[column.name] for column in others),
                    ),
                )
                .where(mine.c[category.name] == source_id)
                .order_by(*(mine.c[column.name] for column in others))
            )
            clashes = session.execute(query).all()
            if not clashes:
                continue
            label = table.info.get("label", table_name.replace("_", " "))
            where = ", ".join(" ".join(_clash_text(value) for value in row) for row in clashes)
            raise ValueError(
                f"{target_name} already has a {label} for {where}, so {source_name}'s {label} "
                "cannot move. Change or delete one of them first."
            )


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
            after = dict.fromkeys(before, target_id)
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


def _rental_mark_changes(session: Session, source_id: int, target_id: int) -> list[Change]:
    """A rental category moved into one that is not makes the target a rental category, so the
    rental report keeps counting what moved."""
    source = session.get(Category, source_id)
    target = snapshot(session, "category", target_id)
    if source is None or target is None or not source.rental or target["rental"]:
        return []
    return [Change("category", str(target_id), target, {**target, "rental": True})]


def _costco_food_changes(session: Session, source_id: int, target_id: int) -> list[Change]:
    """The Costco food setting names the target when it named the source. It is a batch change,
    so undo puts it back."""
    before = snapshot(session, "preference", COSTCO_FOOD_CATEGORY)
    if before is None or before.get("value") != source_id:
        return []
    return [Change("preference", COSTCO_FOOD_CATEGORY, before, {**before, "value": target_id})]


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


# The conditions a rule has, in the order a draft names them, and the fields a rule stores.
_CONDITIONS = (
    "paid_by",
    "below_cents",
    "at_least_cents",
    "counterparty",
    "contact_id",
    "account_id",
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
