"""The Recurring page's add and edit forms: their fields as typed, checked and turned into the
values the recurring writes take. Each check raises ValueError with a message the form shows."""

from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from omsync.core.money import format_cents
from omsync.ledger.categories_admin import category_choices
from omsync.ledger.models import Account, Category, Transaction
from omsync.plan.models import Cadence, RecurringSeries
from omsync.plan.recurring import (
    amount_mode,
    current_amount,
    edited_amount,
    fixed_tolerance_cents,
    observed_tolerance_cents,
    payee_pattern_for,
    price_history,
)
from omsync.web.amounts import (
    parse_date,
    parse_positive_cents,
    parse_tolerance_cents,
)
from omsync.web.recurring_view import account_choices, noun_for, plain_dollars


def _name(text: str) -> str:
    clean = text.strip()
    if not clean:
        raise ValueError("Give it a name.")
    return clean


def _payee(text: str) -> str:
    """The payee pattern a typed payee makes, the one a bill started from a transaction gets."""
    try:
        return payee_pattern_for(text)
    except ValueError:
        raise ValueError("Enter the payee as it appears on statements.") from None


def _mode(text: str) -> str:
    if text not in ("fixed", "varies"):
        raise ValueError("Pick Fixed or Varies for the amount.")
    return text


def _category(db: Session, text: str) -> int | None:
    if not text.strip():
        return None
    try:
        category_id = int(text)
    except ValueError:
        raise ValueError("Pick a category from the list.") from None
    if db.get(Category, category_id) is None:
        raise ValueError("Pick a category from the list.")
    return category_id


def _account(db: Session, text: str) -> int | None:
    """None for Any account; otherwise the ID of an account that exists."""
    if not text.strip():
        return None
    try:
        account_id = int(text)
    except ValueError:
        raise ValueError("Pick an account from the list.") from None
    if db.get(Account, account_id) is None:
        raise ValueError("Pick an account from the list.")
    return account_id


def _cadence(text: str) -> Cadence:
    try:
        return Cadence(text)
    except ValueError:
        raise ValueError("Pick a cadence from the list.") from None


def _has_prices(db: Session, series: RecurringSeries) -> bool:
    """Whether the bill has accepted new amounts. Its amount then comes from them, not the form."""
    return len(price_history(db, series)) > 1


def add_values(
    db: Session,
    *,
    name: str,
    payee: str,
    account_id: str,
    cadence: str,
    amount: str,
    income: str,
    starts_on: str,
    category_id: str,
) -> dict[str, Any]:
    magnitude = parse_positive_cents(amount)
    return {
        "name": _name(name),
        "payee_pattern": _payee(payee),
        "account_id": _account(db, account_id),
        "cadence": _cadence(cadence),
        "amount_cents": magnitude if income else -magnitude,
        "starts_on": parse_date(starts_on, "Pick the first due date."),
        "category_id": _category(db, category_id),
    }


def edit_values(
    db: Session,
    series: RecurringSeries,
    *,
    name: str,
    cadence: str,
    amount: str,
    mode: str,
    tolerance: str,
    category_id: str,
) -> dict[str, Any]:
    """The edit form's values as update_series takes them. A bill with accepted new amounts keeps
    its amount, so no amount is posted (None). The tolerance is the exact one when Advanced is
    filled, else the Fixed or Varies one. Fixed is measured from the amount in effect after the
    save."""
    amount_cents = (
        None if _has_prices(db, series) else edited_amount(series, parse_positive_cents(amount))
    )
    exact = parse_tolerance_cents(tolerance) if tolerance.strip() else None
    if exact is not None:
        tolerance_cents = exact
    elif _mode(mode) == "fixed":
        effective = current_amount(db, series) if amount_cents is None else amount_cents
        tolerance_cents = fixed_tolerance_cents(effective)
    else:
        tolerance_cents = observed_tolerance_cents(db, series)
    return {
        "name": _name(name),
        "cadence": _cadence(cadence),
        "amount_cents": amount_cents,
        "amount_tolerance_cents": tolerance_cents,
        "category_id": _category(db, category_id),
    }


def has_changes(series: RecurringSeries, values: dict[str, Any]) -> bool:
    return any(
        [
            values["name"] != series.name,
            values["cadence"] != series.cadence,
            values["amount_cents"] is not None and values["amount_cents"] != series.amount_cents,
            values["amount_tolerance_cents"] != series.amount_tolerance_cents,
            values["category_id"] != series.category_id,
        ]
    )


def edit_bill_form(
    db: Session,
    series: RecurringSeries,
    *,
    key: str,
    name: str | None = None,
    cadence: str | None = None,
    amount: str | None = None,
    mode: str | None = None,
    tolerance: str | None = None,
    category_id: str | None = None,
    error: str = "",
) -> dict[str, Any]:
    current_category = "" if series.category_id is None else str(series.category_id)
    return {
        "series_id": series.id,
        "noun": noun_for(series.amount_cents),
        "name": series.name if name is None else name,
        "cadence": series.cadence.value if cadence is None else cadence,
        "has_prices": _has_prices(db, series),
        "current": format_cents(current_amount(db, series)),
        "amount": plain_dollars(series.amount_cents) if amount is None else amount,
        "mode": amount_mode(series) if mode is None else mode,
        "tolerance": "" if tolerance is None else tolerance,
        "tolerance_text": format_cents(series.amount_tolerance_cents),
        "category_id": current_category if category_id is None else category_id,
        "error": error,
        "key": key,
        "choices": category_choices(db),
    }


def new_bill_form(
    db: Session,
    *,
    key: str,
    today: date,
    name: str = "",
    payee: str = "",
    account_id: str = "",
    cadence: str = "monthly",
    amount: str = "",
    income: str = "",
    starts_on: str = "",
    category_id: str = "",
    error: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "payee": payee,
        "account_id": account_id,
        "cadence": cadence,
        "amount": amount,
        "income": income,
        "starts_on": starts_on or today.isoformat(),
        "category_id": category_id,
        "error": error,
        "key": key,
        "accounts": account_choices(db),
        "choices": category_choices(db),
    }


def prefill(db: Session, transaction_id: int | None) -> dict[str, str]:
    """The add form's fields for a transaction: its payee as the name and as the payee on
    statements, its account, its amount and sign, its posted day as the first due date, and its
    category. A missing transaction leaves the form blank."""
    txn = db.get(Transaction, transaction_id) if transaction_id is not None else None
    if txn is None:
        return {}
    return {
        "name": txn.payee,
        "payee": txn.payee_raw,
        "account_id": str(txn.account_id),
        "amount": plain_dollars(txn.amount_cents),
        "income": "1" if txn.amount_cents > 0 else "",
        "starts_on": txn.posted_on.isoformat(),
        "category_id": "" if txn.category_id is None else str(txn.category_id),
    }
