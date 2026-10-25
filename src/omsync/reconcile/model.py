"""Values reconciliation works with: the transaction view a rule sees, and the draft it proposes.

Nothing here touches the database. A draft becomes a proposal only when a later step saves it.
Money is integer cents throughout.
"""

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from omsync.ledger.categories import CategoryType
from omsync.ledger.contacts import ContactRole
from omsync.ledger.links import LinkKind
from omsync.ledger.sources import AccountType


class Queue(StrEnum):
    """Where a proposal waits.

    auto_approve: the match is exact and unambiguous.
    review: the match fits every rule but needs a look.
    manual: several candidates fit, so you must choose.
    """

    auto_approve = "auto_approve"
    review = "review"
    manual = "manual"


@dataclass(frozen=True)
class TxnView:
    """A transaction as a rule sees it.

    amount_cents is signed, and negative means money out. Inside a Pool it is the part of the
    amount that no link explains yet. For an unlinked transaction that is the whole amount.

    description is the text rules match: the statement row's description, as import reads it. It
    comes from the statement row, never from Transaction.memo, which you and omsync edit.
    payee_raw is the transaction's raw payee, which the payee and the receipt checks read.

    contact_id, contact_account_id, and contact_role are the other side stored on the transaction
    (see omsync.ledger.other_side), and the contact's role.
    """

    id: int
    account_id: int
    account_type: AccountType
    account_last4: str | None
    account_joint: bool
    posted_on: date
    amount_cents: int
    payee_raw: str
    description: str
    payee: str
    memo: str | None
    category_name: str | None
    category_type: CategoryType | None
    contact_id: int | None = None
    contact_account_id: int | None = None
    contact_role: ContactRole | None = None

    @property
    def with_household(self) -> bool:
        """True when the stored other side is the owner or the co-owner of the joint accounts."""
        return self.contact_role in (ContactRole.owner, ContactRole.co_owner)


@dataclass(frozen=True)
class AccountRef:
    """How descriptions refer to one tracked account.

    labels are the names people write (the account name, with and without its institution).
    digits are the last four of the account number and of its current statement ID, which differ
    once a card is reissued. joint marks an account shared with your partner.
    """

    id: int
    labels: tuple[str, ...]
    digits: frozenset[str]
    joint: bool = False


@dataclass(frozen=True)
class Draft:
    """A proposed link from one money-out transaction to one money-in transaction.

    out_txn_id is the debit and in_txn_id is the credit. Either may be None for a one-sided
    proposal, such as a joint settlement whose joint inflow is missing. amount_cents is the
    positive amount the link explains. It never exceeds the open amount of either side.
    """

    kind: LinkKind
    rule_key: str
    out_txn_id: int | None
    in_txn_id: int | None
    amount_cents: int
    confidence: float
    queue: Queue
    evidence: dict[str, Any]

    def __post_init__(self) -> None:
        if self.out_txn_id is None and self.in_txn_id is None:
            raise ValueError("a draft needs at least one side")
        if isinstance(self.amount_cents, bool) or not isinstance(self.amount_cents, int):
            raise TypeError("draft amount must be integer cents")
        if self.amount_cents <= 0:
            raise ValueError("draft amount must be positive; the sign is carried by the sides")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


class ExplainedBy(StrEnum):
    """Why a transaction that is expected to have a counterpart needs no decision from you.

    contact: its other side is a stored contact, recognized or chosen.
    named: it is a payment naming a person you have no contact for; the name is the other side.
    Neither is a link, so neither reconciles the transaction.
    """

    contact = "contact"
    named = "named"


@dataclass(frozen=True)
class NotMatched:
    """A transaction expected to have a counterpart that no link or proposal accounts for, and
    whose other side is unknown. txn carries the amount no link explains yet."""

    txn: TxnView


@dataclass(frozen=True)
class Explained:
    """A transaction that would be Not matched, with the reason it needs nothing from you.

    contact_id is the stored contact, for an explanation by contact.
    """

    txn: TxnView
    by: ExplainedBy
    reason: str
    contact_id: int | None = None


def pair_signature(first_id: int | None, second_id: int | None, amount_cents: int) -> str:
    """Return the kind-free key of a set of transactions and an amount: sorted IDs, then amount.

    Rejections are stored by this key, so a pair rejected as a transfer is also suppressed as
    P2P or any other kind. A one-sided proposal has a None for the missing side. The key must not
    change between runs.
    """
    present = sorted(txn_id for txn_id in (first_id, second_id) if txn_id is not None)
    ids = ",".join(str(txn_id) for txn_id in present)
    return f"{ids}|{amount_cents}"


def signature(draft: Draft) -> str:
    """Return the signature that identifies a draft: its kind, then its pair key."""
    key = pair_signature(draft.out_txn_id, draft.in_txn_id, draft.amount_cents)
    return f"{draft.kind.value}|{key}"


def pair_signature_of(draft_signature: str) -> str:
    """Return the kind-free pair key inside a draft signature."""
    _, _, key = draft_signature.partition("|")
    return key
