"""The register's state: the account it shows, the filter, the saved filter in use, and the page.

The filter travels in the URL as base64url JSON in `f`, and the page as `page`, so a reload or Back
shows the same rows. Every request also carries the state in a hidden form (#register-state), so an
htmx request can send it with hx-include. A reply re-renders that form, so the next request reads
fresh values. The rows per page are a saved preference, not part of the URL.
"""

import base64
from dataclasses import dataclass, field
from urllib.parse import urlencode
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.money import format_cents
from omsync.ledger.bulk import count_matching
from omsync.ledger.categories import Category
from omsync.ledger.filters import (
    Condition,
    FilterError,
    FilterSpec,
    SavedFilterInfo,
    from_json,
    get_filter,
    list_filters,
    to_json,
)
from omsync.ledger.preferences import get_pref, set_pref
from omsync.ledger.register import RegisterRow, categories, list_transactions, open_review_count
from omsync.ledger.sources import Account

MAX_FILTER_LENGTH = 8000
DATE_FORMAT = "%b %-d, %Y"
PAGE_SIZES = (25, 50, 100, 200)
PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_PREF = "register_page_size"


@dataclass(frozen=True)
class RegisterState:
    """Everything a register page or fragment shows about the current view, computed once."""

    account: Account | None
    spec: FilterSpec
    saved: SavedFilterInfo | None
    saved_modified: bool
    saved_items: tuple[SavedFilterInfo, ...]
    accounts: tuple[Account, ...]  # open accounts, for the account choices
    categories: tuple[tuple[int, str], ...]  # visible categories, for the category choices
    category_names: dict[int, str]  # every category, so a chip can name a hidden one too
    account_names: dict[int, str]
    matching: int
    page: int  # 1-based, and never past the last page
    page_size: int
    review_count: int
    # Sent with the save, update, and delete requests, so a double submit applies once.
    form_key: str = field(default_factory=lambda: uuid4().hex)

    @property
    def account_id(self) -> int | None:
        return None if self.account is None else self.account.id

    @property
    def show_account(self) -> bool:
        return self.account is None

    @property
    def f(self) -> str | None:
        return None if self.spec.is_empty else encode_filter(self.spec)

    @property
    def search(self) -> str | None:
        return self.spec.search

    @property
    def filtered(self) -> bool:
        return not self.spec.is_empty

    @property
    def pages(self) -> int:
        return page_count(self.matching, self.page_size)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def first_row(self) -> int:
        return self.offset + 1

    @property
    def last_row(self) -> int:
        return min(self.offset + self.page_size, self.matching)

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def query(self) -> str:
        """The state as a query string, for links. Empty values are left out."""
        return query_string(
            self.account_id, self.spec, None if self.saved is None else self.saved.id, self.page
        )

    @property
    def url(self) -> str:
        query = self.query
        return "/transactions" + (f"?{query}" if query else "")

    @property
    def scope_url(self) -> str:
        """The register for this account with no filter, saved filter, or search."""
        return "/transactions" + (f"?account={self.account_id}" if self.account_id else "")

    @property
    def chips(self) -> list[tuple[int, str]]:
        return [
            (index, chip_text(condition, self.account_names, self.category_names))
            for index, condition in enumerate(self.spec.conditions)
        ]


def query_string(
    account_id: int | None, spec: FilterSpec, saved_id: int | None, page: int = 1
) -> str:
    """The account, filter, saved filter, and page as a query string. Empty values and the first
    page are left out."""
    params: list[tuple[str, str]] = []
    if account_id is not None:
        params.append(("account", str(account_id)))
    if not spec.is_empty:
        params.append(("f", encode_filter(spec)))
    if saved_id is not None:
        params.append(("saved", str(saved_id)))
    if page > 1:
        params.append(("page", str(page)))
    return urlencode(params)


def build_state(
    db: Session,
    *,
    account_id: int | None,
    spec: FilterSpec,
    saved_id: int | None = None,
    page: int = 1,
) -> RegisterState:
    """Read what the register shows for this scope. A page before the first or past the last is
    the nearest one. Raises LookupError for an unknown account."""
    account = None
    if account_id is not None:
        account = db.get(Account, account_id)
        if account is None:
            raise LookupError(f"There is no account {account_id}.")
    saved = None
    if saved_id is not None:
        try:
            saved = get_filter(db, saved_id)
        except LookupError:
            saved = None  # a saved filter that was deleted is simply not in use
    accounts = tuple(
        db.scalars(select(Account).where(Account.closed.is_(False)).order_by(Account.name)).all()
    )
    account_names = {row.id: row.name for row in db.scalars(select(Account))}
    category_names = {row.id: row.name for row in db.scalars(select(Category))}
    matching = count_matching(db, spec, account_id=account_id)
    size = page_size(db)
    return RegisterState(
        account=account,
        spec=spec,
        saved=saved,
        saved_modified=saved is not None and saved.spec != spec,
        saved_items=tuple(list_filters(db)),
        accounts=accounts,
        categories=tuple(categories(db)),
        category_names=category_names,
        account_names=account_names,
        matching=matching,
        page=min(max(page, 1), page_count(matching, size)),
        page_size=size,
        review_count=open_review_count(db),
    )


def page_count(matching: int, size: int) -> int:
    """How many pages `matching` rows make at `size` rows a page. An empty register has one."""
    return max(1, -(-matching // size))


def page_rows(db: Session, state: RegisterState) -> list[RegisterRow]:
    """The rows on the state's page."""
    return list_transactions(
        db,
        account_id=state.account_id,
        spec=state.spec,
        offset=state.offset,
        limit=state.page_size,
    )


def page_size(session: Session) -> int:
    """Rows per page. A missing or unknown stored value means the default."""
    value = get_pref(session, PAGE_SIZE_PREF, PAGE_SIZE_DEFAULT)
    return value if value in PAGE_SIZES else PAGE_SIZE_DEFAULT


def save_page_size(session: Session, size: int) -> None:
    set_pref(session, PAGE_SIZE_PREF, size)


def encode_filter(spec: FilterSpec) -> str:
    """The spec as base64url JSON, with padding left off."""
    raw = to_json(spec).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_filter(text: str | None) -> FilterSpec:
    """The spec in an `f` value. A missing value is an empty spec. Raises FilterError for a value
    that cannot be read, with a message that says how to recover."""
    if not text:
        return FilterSpec()
    if len(text) > MAX_FILTER_LENGTH:
        raise FilterError(_unreadable("it is too long."))
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
        return from_json(raw.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:  # binascii errors are ValueErrors
        reason = str(exc) if isinstance(exc, FilterError) else "it is not a filter."
        raise FilterError(_unreadable(reason)) from exc


def _unreadable(reason: str) -> str:
    return f"This filter link can't be read: {reason} Clear the filters to start again."


def chip_text(
    condition: Condition, account_names: dict[int, str], category_names: dict[int, str]
) -> str:
    """A condition in words, for its chip."""
    field, op, value = condition.field, condition.op, condition.value
    if field == "date":
        if op == "between":
            return f"Date between {_day(value[0])} and {_day(value[1])}"
        return f"Date {_op_words(op)} {_day(value)}"
    if field == "account":
        names = ", ".join(account_names.get(item, f"account {item}") for item in value)
        return f"Account {'is' if op == 'is' else 'is not'} {names}"
    if field == "payee":
        return f"Payee {_op_words(op)} “{value}”"
    if field == "category":
        if op == "is_empty":
            return "Category is empty"
        names = ", ".join(category_names.get(item, f"category {item}") for item in value)
        return f"Category {'is' if op == 'is' else 'is not'} {names}"
    if field == "amount":
        if op == "between":
            return f"Amount between {format_cents(value[0])} and {format_cents(value[1])}"
        return f"Amount {_op_words(op)} {format_cents(value)}"
    if field == "memo":
        return "Memo is empty" if op == "is_empty" else f"Memo contains “{value}”"
    return f"Origin is {value}"


def _day(value: object) -> str:
    return value.strftime(DATE_FORMAT)  # type: ignore[attr-defined]


_OP_WORDS = {
    "is": "is",
    "after": "after",
    "before": "before",
    "is_not": "is not",
    "contains": "contains",
    "not_contains": "does not contain",
    "gt": "is greater than",
    "lt": "is less than",
}


def _op_words(op: str) -> str:
    return _OP_WORDS.get(op, op)


def category_id_from(text: str) -> int | None:
    """The category id a picker sent. Blank means no category. Raises ValueError for anything that
    is not an id."""
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError:
        raise ValueError("Pick a category from the list.") from None
