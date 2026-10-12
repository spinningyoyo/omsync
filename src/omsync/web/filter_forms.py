"""The filter builder's form: which fields and operations it offers, how a condition fills it in,
and how the submitted form becomes a condition. Presentation only; the rules are in
omsync.ledger.filters."""

from dataclasses import dataclass, field

from omsync.core.money import format_cents, to_cents
from omsync.ledger.filters import FIELD_OPS, Condition, FilterError

FIELD_LABELS: dict[str, str] = {
    "date": "Date",
    "account": "Account",
    "payee": "Payee",
    "category": "Category",
    "amount": "Amount",
    "memo": "Memo",
    "origin": "Origin",
}

OP_LABELS: dict[str, dict[str, str]] = {
    "date": {"is": "is", "after": "is after", "before": "is before", "between": "is between"},
    "account": {"is": "is", "is_not": "is not"},
    "payee": {"contains": "contains", "is": "is", "not_contains": "does not contain"},
    "category": {"is": "is", "is_not": "is not", "is_empty": "is empty"},
    "amount": {"is": "is", "gt": "is greater than", "lt": "is less than", "between": "is between"},
    "memo": {"contains": "contains", "is_empty": "is empty"},
    "origin": {"is": "is"},
}


@dataclass
class FormValues:
    """What the filter form shows. Strings as typed, and ids as the checkboxes give them."""

    field: str
    op: str
    index: str = ""
    value: str = ""
    value_to: str = ""
    amount: str = ""
    amount_to: str = ""
    origin: str = "manual"
    account_ids: list[int] = field(default_factory=list)
    category_ids: list[int] = field(default_factory=list)
    error: str = ""

    @property
    def is_edit(self) -> bool:
        return self.index != ""


def field_choices() -> list[tuple[str, str]]:
    return [(key, FIELD_LABELS[key]) for key in FIELD_OPS]


def op_choices(field_name: str) -> list[tuple[str, str]]:
    return [(op, OP_LABELS[field_name][op]) for op in FIELD_OPS[field_name]]


def values_for(condition: Condition, index: int) -> FormValues:
    """Prefill the form from an existing condition, so it can be changed."""
    values = FormValues(field=condition.field, op=condition.op, index=str(index))
    value = condition.value
    if condition.field == "date":
        if condition.op == "between":
            values.value, values.value_to = value[0].isoformat(), value[1].isoformat()
        elif value is not None:
            values.value = value.isoformat()
    elif condition.field == "account":
        values.account_ids = list(value)
    elif condition.field == "category":
        values.category_ids = list(value or ())
    elif condition.field == "amount":
        if condition.op == "between":
            values.amount, values.amount_to = _plain(value[0]), _plain(value[1])
        else:
            values.amount = _plain(value)
    elif condition.field == "origin":
        values.origin = value
    elif value is not None:
        values.value = value
    return values


def blank_values(field_name: str, index: str = "") -> FormValues:
    """An empty form for a field, with its first operation chosen."""
    return FormValues(field=field_name, op=FIELD_OPS[field_name][0], index=index)


def condition_from(values: FormValues) -> Condition:
    """Turn the submitted form into a condition. Raises FilterError with a message for the form."""
    field_name, op = values.field, values.op
    if field_name not in FIELD_OPS or op not in FIELD_OPS[field_name]:
        raise FilterError("Choose a field and an operation.")
    if field_name == "date":
        if op == "between":
            return Condition(
                "date",
                op,
                [_required(values.value, "a date"), _required(values.value_to, "a second date")],
            )
        return Condition("date", op, _required(values.value, "a date"))
    if field_name == "account":
        return Condition("account", op, values.account_ids)
    if field_name == "category":
        if op == "is_empty":
            return Condition("category", op)
        return Condition("category", op, values.category_ids)
    if field_name == "payee":
        return Condition("payee", op, _required(values.value, "some text"))
    if field_name == "memo":
        if op == "is_empty":
            return Condition("memo", op)
        return Condition("memo", op, _required(values.value, "some text"))
    if field_name == "amount":
        if op == "between":
            return Condition("amount", op, [_cents(values.amount), _cents(values.amount_to)])
        return Condition("amount", op, _cents(values.amount))
    return Condition("origin", "is", values.origin)


def _required(text: str, what: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        raise FilterError(f"Enter {what}.")
    return cleaned


def _cents(text: str) -> int:
    cleaned = (text or "").strip()
    if not cleaned:
        raise FilterError("Enter an amount.")
    try:
        return to_cents(cleaned)
    except (TypeError, ValueError):
        raise FilterError(f"{cleaned} is not an amount.") from None


def _plain(cents: int) -> str:
    """An amount as the form shows it: dollars and cents, with no symbol or grouping."""
    return format_cents(cents, symbol="").replace(",", "")
