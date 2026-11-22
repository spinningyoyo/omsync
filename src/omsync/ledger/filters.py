"""Transaction filters: conditions, how they combine, search, and saved filters.

A FilterSpec is a set of conditions joined by "all" (AND) or "any" (OR), plus an optional search
text that always narrows the result. It is plain data. to_json and from_json round-trip it, and
from_json raises FilterError for anything that is not a valid spec, so a hand-edited URL is
rejected rather than guessed at. apply() adds a spec to a select of Transaction as where-clauses,
so listing, paging, and bulk selection share one query.

Category and origin follow Actual. A transaction matches "category is X" when its own category is
X or one of its splits has X. A split transaction's origin is its splits' origins, so "origin is
manual" matches a split you set by hand.

Dates: "after" and "before" are exclusive and "between" is inclusive. Amounts are integer cents
with their sign, as stored, so spending is negative. Text matches ignore case for ASCII letters,
which is what SQLite's lower() folds.

Saved filters live in the saved_filter table, and every change to it is a batch.
"""

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from sqlalchemy import Select, and_, func, not_, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    Change,
    applied_batch_for_key,
    make_preview,
    next_primary_key,
    snapshot,
)
from omsync.core.batches import apply as apply_batch
from omsync.core.months import parse_day
from omsync.ledger.categories import SavedFilter
from omsync.ledger.tags import SplitTag, Tag, TransactionTag
from omsync.ledger.transactions import CategoryOrigin, Split, Transaction


class FilterError(ValueError):
    """A filter or a saved filter is not valid. The message is safe to show to you."""


FIELD_OPS: dict[str, tuple[str, ...]] = {
    "date": ("is", "after", "before", "between"),
    "account": ("is", "is_not"),
    "payee": ("contains", "is", "not_contains"),
    "category": ("is", "is_not", "is_empty"),
    "tag": ("is", "is_not", "is_empty"),
    "amount": ("is", "gt", "lt", "between"),
    "memo": ("contains", "is_empty"),
    "origin": ("is",),
    "transaction": ("is",),
}
PICKED_FIELDS = frozenset({"transaction"})
"""Fields a link sets, such as the two transactions of a pair. The filter form never offers them."""
VALUELESS_OPS = frozenset({"is_empty"})
MATCHES = ("all", "any")
SEARCH_MAX = 200
NAME_MAX = 60
ORIGINS = tuple(origin.value for origin in CategoryOrigin)


@dataclass(frozen=True)
class Condition:
    """One test on one field. Values are normalized: dates are date objects, id lists are sorted
    tuples, and amounts are integer cents. Invalid combinations raise FilterError."""

    field: str
    op: str
    value: Any = None

    def __post_init__(self) -> None:
        if self.field not in FIELD_OPS:
            raise FilterError(f"{self.field!r} is not a field you can filter by.")
        if self.op not in FIELD_OPS[self.field]:
            raise FilterError(f"{self.op!r} is not an operation for {self.field}.")
        object.__setattr__(self, "value", _normal_value(self.field, self.op, self.value))


@dataclass(frozen=True)
class FilterSpec:
    """Conditions joined by match ("all" or "any"), and a search text that always applies."""

    conditions: tuple[Condition, ...] = ()
    match: Literal["all", "any"] = "all"
    search: str | None = None

    def __post_init__(self) -> None:
        conditions = tuple(self.conditions)
        if not all(isinstance(condition, Condition) for condition in conditions):
            raise FilterError("Each condition must be a Condition.")
        if self.match not in MATCHES:
            raise FilterError('Match must be "all" or "any".')
        search = _search_text(self.search)
        object.__setattr__(self, "conditions", conditions)
        object.__setattr__(self, "search", search)

    @property
    def is_empty(self) -> bool:
        return not self.conditions and self.search is None


# Serialization


def to_json(spec: FilterSpec) -> str:
    """The spec as compact JSON text. from_json reads it back to an equal spec."""
    return json.dumps(to_data(spec), separators=(",", ":"), sort_keys=True)


def from_json(text: str) -> FilterSpec:
    """Parse JSON text into a spec. Raises FilterError if it is not valid JSON or not a spec."""
    if not isinstance(text, str):
        raise FilterError("A filter must be text.")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise FilterError("That filter is not valid JSON.") from exc
    return from_data(data)


def to_data(spec: FilterSpec) -> dict[str, Any]:
    """The spec as JSON-safe data, for storing in a saved filter."""
    return {
        "conditions": [
            {"field": c.field, "op": c.op, "value": _json_value(c.value)} for c in spec.conditions
        ],
        "match": spec.match,
        "search": spec.search,
    }


def from_data(data: Any) -> FilterSpec:
    """Build a spec from parsed JSON. Unknown keys, fields, operations, and value types raise."""
    if not isinstance(data, dict):
        raise FilterError("A filter must be an object.")
    unknown = sorted(set(data) - {"conditions", "match", "search"})
    if unknown:
        raise FilterError(f"Unknown filter key {unknown[0]!r}.")
    raw = data.get("conditions", [])
    if not isinstance(raw, list):
        raise FilterError("The conditions must be a list.")
    return FilterSpec(
        conditions=tuple(_condition_from_data(item) for item in raw),
        match=data.get("match", "all"),
        search=data.get("search"),
    )


def _condition_from_data(item: Any) -> Condition:
    if not isinstance(item, dict):
        raise FilterError("Each condition must be an object.")
    unknown = sorted(set(item) - {"field", "op", "value"})
    if unknown:
        raise FilterError(f"Unknown condition key {unknown[0]!r}.")
    field, op = item.get("field"), item.get("op")
    if not isinstance(field, str) or not isinstance(op, str):
        raise FilterError("Each condition needs a field and an operation.")
    return Condition(field, op, item.get("value"))


def _json_value(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    return value


# Value checks


def _normal_value(field: str, op: str, value: Any) -> Any:
    if op in VALUELESS_OPS:
        if value is not None:
            raise FilterError(f"{field} {op.replace('_', ' ')} takes no value.")
        return None
    if value is None:
        raise FilterError(f"{field} {op} needs a value.")
    if field == "date":
        return _date_value(op, value)
    if field in ("account", "transaction") or (
        field in ("category", "tag") and op in ("is", "is_not")
    ):
        return _ids(value)
    if field == "payee" or (field == "memo" and op == "contains"):
        return _text(value)
    if field == "amount":
        return _amount_value(op, value)
    return _origin_value(value)


def _date_value(op: str, value: Any) -> Any:
    if op == "between":
        return _ordered_pair(value, _as_date, "The first date is after the last date.")
    return _as_date(value)


def _amount_value(op: str, value: Any) -> Any:
    if op == "between":
        return _ordered_pair(value, _as_cents, "The first amount is above the last amount.")
    return _as_cents(value)


def _origin_value(value: Any) -> str:
    text = _text(value)
    if text not in ORIGINS:
        raise FilterError(f"{text!r} is not an origin. Use one of {', '.join(ORIGINS)}.")
    return text


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        raise FilterError("Use a date, not a date and time.")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return parse_day(value)
        except ValueError:
            pass
    raise FilterError(f"{value!r} is not a date. Use YYYY-MM-DD.")


def _as_cents(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FilterError("An amount must be a whole number of cents.")
    return value


def _ordered_pair(value: Any, convert: Any, message: str) -> tuple[Any, Any]:
    if not isinstance(value, list | tuple) or len(value) != 2:
        raise FilterError("A range needs exactly two values.")
    low, high = convert(value[0]), convert(value[1])
    if low > high:
        raise FilterError(message)
    return (low, high)


def _ids(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list | tuple) or not value:
        raise FilterError("Choose at least one item.")
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise FilterError("Items must be positive whole-number IDs.")
    return tuple(sorted(set(value)))


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FilterError("Enter some text.")
    return value.strip()


def _search_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise FilterError("Search must be text.")
    text = value.strip()
    if len(text) > SEARCH_MAX:
        raise FilterError(f"Search can be up to {SEARCH_MAX} characters.")
    return text or None


# Applying a spec to a query


def apply(stmt: Select, spec: FilterSpec) -> Select:
    """Add the spec's where-clauses to a select of Transaction. The select is returned, not run."""
    if spec.conditions:
        clauses = [_clause(condition) for condition in spec.conditions]
        stmt = stmt.where(and_(*clauses) if spec.match == "all" else or_(*clauses))
    if spec.search is not None:
        stmt = stmt.where(_search_clause(spec.search))
    return stmt


def _clause(condition: Condition) -> ColumnElement[bool]:
    field, op, value = condition.field, condition.op, condition.value
    if field == "date":
        return _date_clause(op, value)
    if field == "account":
        if op == "is":
            return Transaction.account_id.in_(value)
        return Transaction.account_id.not_in(value)
    if field == "payee":
        return _payee_clause(op, value)
    if field == "category":
        return _category_clause(op, value)
    if field == "tag":
        return _tag_clause(op, value)
    if field == "amount":
        return _amount_clause(op, value)
    if field == "memo":
        return _memo_clause(op, value)
    if field == "transaction":
        return Transaction.id.in_(value)
    return _origin_clause(value)


def _date_clause(op: str, value: Any) -> ColumnElement[bool]:
    column = Transaction.posted_on
    if op == "is":
        return column == value
    if op == "after":
        return column > value
    if op == "before":
        return column < value
    return and_(column >= value[0], column <= value[1])


def _payee_clause(op: str, value: str) -> ColumnElement[bool]:
    contains = or_(
        Transaction.payee.icontains(value, autoescape=True),
        Transaction.payee_raw.icontains(value, autoescape=True),
    )
    if op == "contains":
        return contains
    if op == "not_contains":
        return not_(contains)
    lowered = value.lower()
    return or_(
        func.lower(Transaction.payee) == lowered,
        func.lower(Transaction.payee_raw) == lowered,
    )


def _category_clause(op: str, value: Any) -> ColumnElement[bool]:
    if op == "is_empty":
        # Uncategorized: an unsplit transaction with no category, or a split one with any line
        # that has none, as a receipt leaves an item that matched no keyword.
        has_split = select(Split.id).where(Split.transaction_id == Transaction.id).exists()
        split_without_category = (
            select(Split.id)
            .where(Split.transaction_id == Transaction.id, Split.category_id.is_(None))
            .exists()
        )
        return or_(and_(Transaction.category_id.is_(None), not_(has_split)), split_without_category)
    split_in_ids = (
        select(Split.id)
        .where(Split.transaction_id == Transaction.id, Split.category_id.in_(value))
        .exists()
    )
    if op == "is":
        return or_(Transaction.category_id.in_(value), split_in_ids)
    # Written without NOT over IN: a NULL category makes "IN" unknown, and NOT of unknown drops
    # the row. IS NULL and EXISTS are always true or false, so this is the negation of "is".
    return and_(
        not_(split_in_ids),
        or_(Transaction.category_id.is_(None), Transaction.category_id.not_in(value)),
    )


def _tag_clause(op: str, value: Any) -> ColumnElement[bool]:
    has_txn_tag = select(TransactionTag.tag_id).where(
        TransactionTag.transaction_id == Transaction.id
    )
    has_split_tag = (
        select(SplitTag.tag_id)
        .join(Split, Split.id == SplitTag.split_id)
        .where(Split.transaction_id == Transaction.id)
    )
    if op == "is_empty":
        return and_(not_(has_txn_tag.exists()), not_(has_split_tag.exists()))
    txn_match = has_txn_tag.where(TransactionTag.tag_id.in_(value)).exists()
    split_match = has_split_tag.where(SplitTag.tag_id.in_(value)).exists()
    if op == "is":
        return or_(txn_match, split_match)
    return and_(not_(txn_match), not_(split_match))


def _amount_clause(op: str, value: Any) -> ColumnElement[bool]:
    column = Transaction.amount_cents
    if op == "is":
        return column == value
    if op == "gt":
        return column > value
    if op == "lt":
        return column < value
    return and_(column >= value[0], column <= value[1])


def _memo_clause(op: str, value: Any) -> ColumnElement[bool]:
    if op == "is_empty":
        return or_(Transaction.memo.is_(None), func.trim(Transaction.memo) == "")
    return Transaction.memo.icontains(value, autoescape=True)


def _origin_clause(value: str) -> ColumnElement[bool]:
    origin = CategoryOrigin(value)
    has_split = select(Split.id).where(Split.transaction_id == Transaction.id).exists()
    own = and_(not_(has_split), Transaction.category_origin == origin)
    on_split = (
        select(Split.id)
        .where(Split.transaction_id == Transaction.id, Split.category_origin == origin)
        .exists()
    )
    return or_(own, on_split)


def _search_clause(text: str) -> ColumnElement[bool]:
    matching = Tag.name.icontains(text, autoescape=True)
    on_transaction = (
        select(TransactionTag.id)
        .join(Tag, Tag.id == TransactionTag.tag_id)
        .where(TransactionTag.transaction_id == Transaction.id, matching)
        .exists()
    )
    on_split = (
        select(SplitTag.id)
        .join(Tag, Tag.id == SplitTag.tag_id)
        .join(Split, Split.id == SplitTag.split_id)
        .where(Split.transaction_id == Transaction.id, matching)
        .exists()
    )
    return or_(
        Transaction.payee.icontains(text, autoescape=True),
        Transaction.payee_raw.icontains(text, autoescape=True),
        Transaction.memo.icontains(text, autoescape=True),
        on_transaction,
        on_split,
    )


# Saved filters. Each change is one batch, so it can be undone while nothing has moved on.


@dataclass(frozen=True)
class SavedFilterInfo:
    id: int
    name: str
    spec: FilterSpec


def list_filters(session: Session) -> list[SavedFilterInfo]:
    """Every saved filter, sorted by name without regard to case."""
    rows = session.scalars(select(SavedFilter)).all()
    infos = [_info(row) for row in rows]
    return sorted(infos, key=lambda info: info.name.casefold())


def get_filter(session: Session, filter_id: int) -> SavedFilterInfo:
    """Return one saved filter. Raises LookupError if it does not exist."""
    row = session.get(SavedFilter, filter_id)
    if row is None:
        raise LookupError(f"There is no saved filter {filter_id}.")
    return _info(row)


def save_filter(session: Session, name: str, spec: FilterSpec, *, idempotency_key: str) -> Batch:
    """Save a spec under a new name. Raises FilterError if the name is blank, too long, or taken.

    Names are unique without regard to case. The same key applies at most once.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    clean = _clean_name(name)
    _ensure_name_free(session, clean, except_id=None)
    new_id = next_primary_key(session, SavedFilter.__table__)
    change = Change(
        "saved_filter",
        str(new_id),
        None,
        {"id": new_id, "name": clean, "conditions": to_data(spec)},
    )
    preview = make_preview(
        session,
        "saved_filter.save",
        [change],
        summary={"label": f"Saved filter {clean}", "saved_filter_id": new_id},
    )
    return apply_batch(session, preview, idempotency_key=idempotency_key)


def update_filter(
    session: Session, filter_id: int, spec: FilterSpec, *, idempotency_key: str
) -> Batch:
    """Replace a saved filter's conditions, match, and search. Raises FilterError if nothing
    would change, and LookupError if the filter does not exist."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = snapshot(session, "saved_filter", filter_id)
    if before is None:
        raise LookupError(f"There is no saved filter {filter_id}.")
    after = {**before, "conditions": to_data(spec)}
    if after == before:
        raise FilterError("Nothing has changed since the filter was saved.")
    name = str(before["name"])
    change = Change("saved_filter", str(filter_id), before, after)
    preview = make_preview(
        session,
        "saved_filter.update",
        [change],
        summary={"label": f"Updated filter {name}", "saved_filter_id": filter_id},
    )
    return apply_batch(session, preview, idempotency_key=idempotency_key)


def delete_filter(session: Session, filter_id: int, *, idempotency_key: str) -> Batch:
    """Delete a saved filter. Undo restores it with its id. Raises LookupError if it is missing."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = snapshot(session, "saved_filter", filter_id)
    if before is None:
        raise LookupError(f"There is no saved filter {filter_id}.")
    name = str(before["name"])
    change = Change("saved_filter", str(filter_id), before, None)
    preview = make_preview(
        session,
        "saved_filter.delete",
        [change],
        summary={"label": f"Deleted filter {name}", "saved_filter_id": filter_id},
    )
    return apply_batch(session, preview, idempotency_key=idempotency_key)


def _info(row: SavedFilter) -> SavedFilterInfo:
    return SavedFilterInfo(id=row.id, name=row.name, spec=from_data(row.conditions))


def _clean_name(name: str) -> str:
    clean = (name or "").strip()
    if not clean:
        raise FilterError("Give the filter a name.")
    if len(clean) > NAME_MAX:
        raise FilterError(f"Names can be up to {NAME_MAX} characters.")
    return clean


def _ensure_name_free(session: Session, name: str, *, except_id: int | None) -> None:
    query = select(SavedFilter.id).where(func.lower(SavedFilter.name) == name.lower())
    if except_id is not None:
        query = query.where(SavedFilter.id != except_id)
    if session.scalar(query.limit(1)) is not None:
        raise FilterError(f"A saved filter named {name!r} already exists.")
