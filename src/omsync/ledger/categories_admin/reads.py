"""Reads the Categories page needs: categories, keywords, and rules."""

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.core.money import format_cents, to_cents
from omsync.ledger.categories_admin.helpers import (
    _CONDITIONS,
    _category_row,
    _category_usage,
    _fields_of,
)
from omsync.ledger.categories_admin.rows import (
    _USE_NAMES,
    _WORD,
    CategoryRow,
    CategoryUse,
    KeywordRow,
    RuleRow,
)
from omsync.ledger.categorize import (
    rule_for_transaction,
)
from omsync.ledger.models import (
    Account,
    Category,
    CategoryOrigin,
    CategoryRule,
    CategoryType,
    Contact,
    ContactRole,
    Keyword,
    RuleCounterparty,
    Transaction,
)
from omsync.ledger.payment_methods import (
    PaymentMethod,
    describe_methods,
    parse_paid_by,
)

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


_RULE_FIELDS = (*_CONDITIONS, "to_category_id")


@dataclass(frozen=True)
class _Names:
    """The names a rule's sentence shows, loaded once for a page or a label."""

    categories: dict[int, str]
    hidden: frozenset[int]
    contacts: dict[int, str]
    accounts: dict[int, str]


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
