"""Transactions that should have a counterpart and do not, and the ones whose other side is known.

A transaction is expected to have a counterpart when its description reads as a transfer or a
card payment, names another tracked account, or is a Zelle payment from someone that you filed as
money that does not count. It is Not matched when no link covers all of it, no proposal of the run
involves it, and its other side is unknown. Reconciled stays what links cover; nothing here changes
it. A known other side takes a transaction out of Not matched without a link, and it is shown as
Explained with the other side as the reason: a contact stored on the transaction (see
omsync.ledger.other_side), or a payment that names a person you have no contact for.

assess reads the ledger and writes nothing.
"""

from collections.abc import Sequence

from sqlalchemy.orm import Session

from omsync.ledger.categories import NOT_IN_REPORTS, CategoryType
from omsync.ledger.contacts import ContactRole, ContactView, find_names, load_contacts
from omsync.reconcile.model import Draft, Explained, ExplainedBy, NotMatched, TxnView
from omsync.reconcile.rules.base import Emitter
from omsync.reconcile.rules.p2p import P2P_INFLOW_RE, payment_name_text, written_name
from omsync.reconcile.rules.transfers import ZELLE_PREFIX_RE, is_own_inflow, is_transfer_like


def counterpart_expected(txn: TxnView, emit: Emitter) -> bool:
    """True when the description reads as one half of a pair, so its other half should exist.

    Money out: a transfer between your own accounts (transfer and card-payment wordings, a Zelle
    payment to one of your own names, a named tracked account). Money in: a card-payment credit, a
    transfer wording, a Zelle payment from one of your own names, or a named tracked account. A
    payment to someone else is not one half of anything in this ledger, so it is not expected to
    match. Neither is money in whose category is Income, such as a settlement paid by Zelle.

    A Zelle payment from someone else is expected to match only while it has no category or one
    that reports leave out (Excluded or Owed): a spending category already says what it paid for. A
    Venmo or PayPal payment never is, because its sender writes what they like and nothing in the
    ledger can match it.
    """
    if txn.amount_cents < 0:
        return is_transfer_like(txn, emit)
    if txn.category_type is CategoryType.income:
        return False
    if is_own_inflow(txn, emit):
        return True
    text = txn.description
    if not P2P_INFLOW_RE.search(text) or not ZELLE_PREFIX_RE.search(text):
        return False
    return txn.category_type is None or txn.category_type in NOT_IN_REPORTS


def assess(
    session: Session,
    *,
    candidates: Sequence[TxnView],
    drafts: Sequence[Draft],
    emit: Emitter,
) -> tuple[tuple[NotMatched, ...], tuple[Explained, ...]]:
    """Split the run's open transactions into Not matched and Explained.

    candidates are the transactions open when the run began, with the amount no link explains, and
    the partner contributions that joint settlements recorded. A candidate that a proposal involves
    is neither. One that is expected to have a counterpart is Explained when its other side is
    known and Not matched when it is not. Rows come back in date order.
    """
    contacts = {contact.id: contact for contact in load_contacts(session)}
    drafted = {
        txn_id for draft in drafts for txn_id in (draft.out_txn_id, draft.in_txn_id) if txn_id
    }
    explained: list[Explained] = []
    not_matched: list[NotMatched] = []
    for txn in candidates:
        if txn.id in drafted or not counterpart_expected(txn, emit):
            continue
        stored = None if txn.contact_id is None else contacts.get(txn.contact_id)
        if stored is not None:
            explained.append(
                Explained(
                    txn=txn,
                    by=ExplainedBy.contact,
                    reason=_other_side_reason(txn, stored),
                    contact_id=stored.id,
                )
            )
            continue
        written = _named_person(txn, emit, list(contacts.values()))
        if written is not None:
            explained.append(
                Explained(txn=txn, by=ExplainedBy.named, reason=f"Other side: {written}")
            )
            continue
        not_matched.append(NotMatched(txn=txn))

    return (
        tuple(sorted(not_matched, key=lambda row: (row.txn.posted_on, row.txn.id))),
        tuple(sorted(explained, key=lambda row: (row.txn.posted_on, row.txn.id))),
    )


def _other_side_reason(txn: TxnView, contact: ContactView) -> str:
    if contact.role is ContactRole.owner and txn.contact_account_id is not None:
        for account in contact.accounts:
            if account.id == txn.contact_account_id:
                return f"Your account {account.label}"
    return f"Other side: {contact.name}"


def _named_person(txn: TxnView, emit: Emitter, contacts: Sequence[ContactView]) -> str | None:
    """The person a payment names as written, when you have no contact for them.

    Only a payment from someone else qualifies: money that reads as your own (your names, a
    nickname of one of your accounts, a tracked account) is the case where a missing link is an
    error, and a payment naming the owner or any contact is left to the stored other side.
    """
    if txn.amount_cents < 0 or is_own_inflow(txn, emit):
        return None
    text = payment_name_text(txn.description, txn.amount_cents)
    if text is None or find_names(text, contacts):
        return None
    return written_name(text)
