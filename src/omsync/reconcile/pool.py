"""The pool: the transactions one reconciliation run may still explain.

Rules drain the pool in order. A transaction a rule claims leaves the pool, so a later rule
never sees it. load_pool reads the ledger and writes nothing.
"""

from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import replace
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from omsync.ledger.categories import Category
from omsync.ledger.contacts import Contact
from omsync.ledger.links import Link, LinkKind
from omsync.ledger.sources import Account
from omsync.ledger.statements import source_texts, statement_text
from omsync.ledger.transactions import Transaction
from omsync.reconcile.model import TxnView


class Pool:
    """Transactions still open to the rules, in (posted_on, id) order.

    linked_cents holds, per transaction, the amount that existing links already explain. It is
    recorded in drafts as evidence, so a reviewer can see prior coverage.

    evidence() holds every transaction the range loaded, at its full amount, whether or not links
    explain it. A rule reads it as evidence, such as a bill that its own payment already settled,
    but links only from the open pool. settled_outflows maps each bill that an approved joint
    settlement names to the outflows those settlements link. parked() holds the recorded partner
    contributions: no rule sees them, but you still see them as unmatched. partner_ids() holds
    the partner inflows the joint rule took out of the pool this run, because a proposal names
    them or a settlement already recorded them.
    """

    def __init__(
        self,
        txns: Iterable[TxnView],
        linked_cents: Mapping[int, int],
        *,
        evidence: Iterable[TxnView] = (),
        parked: Iterable[TxnView] = (),
        settled_outflows: Mapping[int, tuple[int, ...]] | None = None,
    ) -> None:
        self._txns: dict[int, TxnView] = {txn.id: txn for txn in txns}
        self._linked_cents: dict[int, int] = dict(linked_cents)
        self._evidence: tuple[TxnView, ...] = tuple(evidence)
        self._parked: tuple[TxnView, ...] = tuple(parked)
        self._partners: set[int] = set()
        self.settled_outflows: dict[int, tuple[int, ...]] = dict(settled_outflows or {})

    def __contains__(self, txn_id: object) -> bool:
        return txn_id in self._txns

    def __len__(self) -> int:
        return len(self._txns)

    def __iter__(self) -> Iterator[TxnView]:
        # A snapshot, so a rule can take transactions while it walks the pool.
        return iter(list(self._txns.values()))

    def evidence(self) -> tuple[TxnView, ...]:
        """Every loaded transaction at its full amount. Read it; do not link from it."""
        return self._evidence

    def parked(self) -> tuple[TxnView, ...]:
        """Recorded partner contributions. Outside every rule; assessed for Not matched like the
        rest, and shown as unmatched."""
        return self._parked

    def take(self, txn_id: int) -> TxnView:
        """Remove a transaction because a rule claimed it. Raises KeyError if it is not here."""
        return self._txns.pop(txn_id)

    def take_partner(self, txn_id: int) -> TxnView:
        """Remove a partner inflow because a joint settlement accounts for it, and remember it."""
        self._partners.add(txn_id)
        return self.take(txn_id)

    def partner_ids(self) -> frozenset[int]:
        return frozenset(self._partners)

    def linked_cents(self, txn_id: int) -> int:
        return self._linked_cents.get(txn_id, 0)


def load_pool(
    session: Session,
    *,
    start: date,
    end: date,
    include_linked: bool = False,
) -> Pool:
    """Load the transactions posted from start to end, inclusive, as a pool.

    Left out, always: split parents (their splits carry the amounts), zero-amount transactions
    (they explain nothing), and partner contributions that an approved joint settlement recorded
    (never linked; they are parked). Left out unless include_linked: transactions that links fully
    explain. A partly linked transaction stays, with only its unlinked remainder as its amount.
    With include_linked, every transaction keeps its full amount, and linked_cents tells you what
    already explains it. evidence() always holds every loaded transaction at full amount.

    A view's description is the statement row's description (see statement_text). It never comes
    from Transaction.memo, which you and omsync edit.
    """
    if end < start:
        raise ValueError("end is before start")

    rows = session.execute(
        select(Transaction, Account, Category.name, Category.type, Contact.role)
        .join(Account, Account.id == Transaction.account_id)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .outerjoin(Contact, Contact.id == Transaction.contact_id)
        .where(
            Transaction.posted_on >= start,
            Transaction.posted_on <= end,
            Transaction.amount_cents != 0,
            ~Transaction.splits.any(),
        )
        .order_by(Transaction.posted_on, Transaction.id)
    ).all()

    coverage = _linked_cents(session, start, end)
    partner_ids, settled_outflows = _joint_settlement_evidence(session)
    sources = source_texts(session, [row[0].id for row in rows])
    everything: list[TxnView] = []
    views: list[TxnView] = []
    parked: list[TxnView] = []
    linked: dict[int, int] = {}
    for txn, account, category_name, category_type, contact_role in rows:
        covered = coverage.get(txn.id, 0)
        if covered:
            linked[txn.id] = covered
        source = sources.get(txn.id)
        full = TxnView(
            id=txn.id,
            account_id=txn.account_id,
            account_type=account.type,
            account_last4=account.last4,
            account_joint=account.joint,
            posted_on=txn.posted_on,
            amount_cents=txn.amount_cents,
            payee_raw=txn.payee_raw,
            description=statement_text(txn.payee_raw, source),
            payee=txn.payee,
            memo=txn.memo,
            category_name=category_name,
            category_type=category_type,
            contact_id=txn.contact_id,
            contact_account_id=txn.contact_account_id,
            contact_role=contact_role,
        )
        everything.append(full)
        if txn.id in partner_ids:
            # A recorded partner contribution is labeled, never linked, so no rule may see it.
            parked.append(full)
            continue
        if include_linked:
            views.append(full)
            continue
        amount = _open_cents(txn.amount_cents, covered)
        if amount != 0:
            views.append(replace(full, amount_cents=amount))
    return Pool(
        views,
        linked,
        evidence=everything,
        parked=parked,
        settled_outflows=settled_outflows,
    )


def _joint_settlement_evidence(
    session: Session,
) -> tuple[frozenset[int], dict[int, tuple[int, ...]]]:
    """Return the partner-contribution IDs that approved joint settlements record, and for each
    bill they name, the outflows they link.

    Read from the links and their evidence, never from memo text, which you may edit. Undoing a
    settlement removes its link, so its partners and bill return to the ordinary rules.
    """
    partners: set[int] = set()
    outflows: dict[int, list[int]] = defaultdict(list)
    rows = session.execute(
        select(Link.from_transaction_id, Link.evidence)
        .where(Link.kind == LinkKind.joint_settlement)
        .order_by(Link.id)
    )
    for outflow_id, evidence in rows:
        if evidence.get("bill_txn_id") is not None:
            bill_outflows = outflows[int(evidence["bill_txn_id"])]
            if outflow_id is not None:
                bill_outflows.append(outflow_id)
        for note in evidence.get("partner_contributions", []):
            partners.add(int(note["txn_id"]))
    return frozenset(partners), {bill: tuple(ids) for bill, ids in outflows.items()}


def _linked_cents(session: Session, start: date, end: date) -> dict[int, int]:
    """Sum the link amounts that touch each transaction posted in the range."""
    in_range = select(Transaction.id).where(
        Transaction.posted_on >= start, Transaction.posted_on <= end
    )
    rows = session.execute(
        select(Link.from_transaction_id, Link.to_transaction_id, Link.amount_cents).where(
            or_(
                Link.from_transaction_id.in_(in_range),
                Link.to_transaction_id.in_(in_range),
            )
        )
    ).all()
    totals: dict[int, int] = defaultdict(int)
    for from_id, to_id, amount in rows:
        for txn_id in {from_id, to_id}:
            if txn_id is not None:
                totals[txn_id] += amount
    return dict(totals)


def _open_cents(amount: int, covered: int) -> int:
    """Return the signed part of amount that no link covers. Zero when it is fully covered."""
    if covered == 0:
        return amount
    remaining = abs(amount) - covered
    if remaining <= 0:
        return 0
    return remaining if amount > 0 else -remaining
