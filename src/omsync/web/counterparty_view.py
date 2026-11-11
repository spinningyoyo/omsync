"""View data for a transaction's detail page: its counterparty and each split's, and the picker.

A read-only assembly of omsync.ledger.counterparty and the register's row. The page's writes are
domain functions called by routes/counterparty.py.
"""

from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.orm import Session

from omsync.ledger import categories_admin as admin
from omsync.ledger.contacts import ContactRole
from omsync.ledger.counterparty import StoredCounterparty, transaction_counterparty
from omsync.ledger.models import CategoryOrigin
from omsync.ledger.register import RegisterRow, SplitRow, get_transaction_row
from omsync.web.contacts_view import (
    ORIGIN_LABELS,
    ChoiceGroup,
    counterparty_choices,
    selected_choice,
)


@dataclass(frozen=True)
class CounterpartyView:
    """The counterparty stored on a transaction or a split, as the page shows it."""

    contact_id: int | None
    name: str
    account: str
    origin: str  # none, recognized, or manual
    origin_label: str
    selected: str  # the picker value that matches it
    is_you: bool


@dataclass(frozen=True)
class SplitView:
    split: SplitRow
    number: int
    counterparty: CounterpartyView
    set_key: str
    clear_key: str


@dataclass(frozen=True)
class TransactionDetail:
    row: RegisterRow
    counterparty: CounterpartyView
    splits: tuple[SplitView, ...]
    set_by_rule: str | None  # the sentence of the rule that sets the category, when a rule does
    groups: tuple[ChoiceGroup, ...]
    set_key: str
    clear_key: str


def load_detail(db: Session, txn_id: int) -> TransactionDetail:
    """Everything the detail page draws. Raises LookupError if the transaction does not exist."""
    row = get_transaction_row(db, txn_id)
    stored = transaction_counterparty(db, txn_id)
    by_split = dict(stored.splits)
    return TransactionDetail(
        row=row,
        counterparty=_view(stored.transaction),
        set_by_rule=_set_by_rule(db, row),
        splits=tuple(
            SplitView(
                split=split,
                number=number,
                counterparty=_view(by_split[split.id]),
                set_key=uuid4().hex,
                clear_key=uuid4().hex,
            )
            for number, split in enumerate(row.splits, start=1)
        ),
        groups=counterparty_choices(db),
        set_key=uuid4().hex,
        clear_key=uuid4().hex,
    )


def _set_by_rule(db: Session, row: RegisterRow) -> str | None:
    """The rule that sets this transaction's category, as its sentence. A rule that set it but no
    longer holds says so, because a categorize has not yet run."""
    if row.is_split or row.category_origin is not CategoryOrigin.rule:
        return None
    rule = admin.transaction_rule(db, row.id)
    return "a rule that no longer holds" if rule is None else rule.sentence


def _view(stored: StoredCounterparty) -> CounterpartyView:
    return CounterpartyView(
        contact_id=stored.contact_id,
        name=stored.contact_name or "",
        account=stored.account_label or "",
        origin=stored.origin.value,
        origin_label=ORIGIN_LABELS[stored.origin.value],
        selected=selected_choice(stored.contact_id, stored.account_id),
        is_you=stored.contact_role is ContactRole.owner,
    )
