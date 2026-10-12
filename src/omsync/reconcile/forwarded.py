"""Payments from a person that you passed on to a joint account.

A friend pays your personal account, and within a week you send the same amount to the joint
account. The outflow is a transfer, so it carries a link of its own, and a link cannot cover more
than a transaction's amount: the inflow can never also be linked to it. This module recognizes the
inflow as explained anyway. It computes on every run and stores nothing, so removing the transfer
link returns the inflow to Not matched.

Money can take hops. Each outflow must be the identical amount, on the account the money is on,
and within FORWARD_WINDOW_DAYS of the inflow. The chain ends at an inflow on a joint account.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.ledger.links import Link, LinkKind
from omsync.reconcile.model import Draft, Queue, TxnView
from omsync.reconcile.pool import load_pool
from omsync.reconcile.rules.p2p import GENERIC_WORDS, P2P_INFLOW_RE

FORWARD_WINDOW_DAYS = 7
# What moves the money on to a joint account: a transfer, or the user's share of a joint bill.
FORWARD_KINDS = frozenset({LinkKind.transfer, LinkKind.joint_settlement})
_MIN_WORD_LENGTH = 3


@dataclass(frozen=True)
class Forward:
    """The route a payment took to a joint account.

    outflows are the transactions that carried it, the first on the payment's own account and the
    last the one that reached the joint account. arrival is the inflow on the joint account.
    """

    outflows: tuple[TxnView, ...]
    arrival: TxnView
    shared_words: tuple[str, ...]

    @property
    def via(self) -> tuple[int, ...]:
        """The accounts the money passed through on the way, in order."""
        return tuple(out.account_id for out in self.outflows[1:])


@dataclass(frozen=True)
class ForwardOutcome:
    """What was found for one inflow: its forward, or the reason there is none to name."""

    forward: Forward | None = None
    hint: str | None = None


def is_p2p_inflow(txn: TxnView) -> bool:
    return txn.amount_cents > 0 and bool(P2P_INFLOW_RE.search(txn.description))


def find_forwards(
    session: Session,
    inflows: Sequence[TxnView],
    *,
    drafts: Sequence[Draft],
    start: date,
    end: date,
) -> dict[int, ForwardOutcome]:
    """Find, for each of these inflows, the forward that explains it.

    An inflow is explained when exactly one chain of identical-amount outflows leaves its account
    within the window and ends on a joint account, each outflow being the sending side of a
    transfer or joint settlement: a link, or an automatic proposal of this run. With several
    chains, no chain is chosen and the outcome carries a hint naming them. One outflow explains at
    most one inflow: when another inflow could use any outflow of the chain, neither is explained.

    start and end are the run's range. Transactions up to the window's length beyond it are read
    too, because a forward can post after the range ends.
    """
    window = timedelta(days=FORWARD_WINDOW_DAYS)
    pool = load_pool(session, start=start - window, end=end + window, include_linked=True)
    everything = {txn.id: txn for txn in pool.evidence()}
    drafted = {
        txn_id for draft in drafts for txn_id in (draft.out_txn_id, draft.in_txn_id) if txn_id
    }
    candidates = [
        txn
        for txn in everything.values()
        if is_p2p_inflow(txn) and not pool.linked_cents(txn.id) and txn.id not in drafted
    ]
    outflows: dict[tuple[int, int], list[TxnView]] = {}
    for txn in everything.values():
        if txn.amount_cents < 0:
            outflows.setdefault((txn.account_id, -txn.amount_cents), []).append(txn)
    onward = _onward_transfers(session, everything, drafts)

    chains = {
        txn.id: _chains(txn, outflows=outflows, onward=onward, everything=everything)
        for txn in candidates
    }
    claimed: dict[int, set[int]] = {}
    for inflow_id, found in chains.items():
        for path, _ in found:
            for out in path:
                claimed.setdefault(out.id, set()).add(inflow_id)
    return {
        inflow.id: _outcome(inflow, chains.get(inflow.id, []), claimed, everything)
        for inflow in inflows
    }


def _chains(
    inflow: TxnView,
    *,
    outflows: Mapping[tuple[int, int], Sequence[TxnView]],
    onward: Mapping[int, Sequence[int]],
    everything: Mapping[int, TxnView],
) -> list[tuple[tuple[TxnView, ...], TxnView]]:
    """Every chain of outflows from the inflow's account that ends on a joint account."""
    last = inflow.posted_on + timedelta(days=FORWARD_WINDOW_DAYS)
    found: list[tuple[tuple[TxnView, ...], TxnView]] = []

    def walk(account_id: int, so_far: tuple[TxnView, ...], visited: frozenset[int]) -> None:
        for out in outflows.get((account_id, inflow.amount_cents), ()):
            if not inflow.posted_on <= out.posted_on <= last:
                continue
            for arrival_id in onward.get(out.id, ()):
                arrival = everything.get(arrival_id)
                if (
                    arrival is None
                    or arrival.amount_cents != inflow.amount_cents
                    or not inflow.posted_on <= arrival.posted_on <= last
                ):
                    continue
                if arrival.account_joint:
                    found.append(((*so_far, out), arrival))
                elif arrival.account_id not in visited:
                    walk(arrival.account_id, (*so_far, out), visited | {arrival.account_id})

    walk(inflow.account_id, (), frozenset({inflow.account_id}))
    return found


def _outcome(
    inflow: TxnView,
    mine: Sequence[tuple[tuple[TxnView, ...], TxnView]],
    claimed: Mapping[int, set[int]],
    everything: Mapping[int, TxnView],
) -> ForwardOutcome:
    if not mine:
        return ForwardOutcome()
    if len(mine) > 1:
        named = ", ".join(_describe(path) for path, _ in mine)
        return ForwardOutcome(hint=f"Several chains of outflows could be the forward: {named}")
    path, arrival = mine[0]
    rivals = sorted({other for out in path for other in claimed[out.id]} - {inflow.id})
    if rivals:
        named = ", ".join(
            f"#{everything[other].id} ({everything[other].posted_on.isoformat()})"
            for other in rivals
        )
        return ForwardOutcome(
            hint=f"The forward {_describe(path)} could also explain the payment {named}"
        )
    return ForwardOutcome(
        forward=Forward(
            outflows=path,
            arrival=arrival,
            shared_words=shared_words(inflow.description, path[0].description),
        )
    )


def _describe(path: Sequence[TxnView]) -> str:
    return " then ".join(f"#{out.id} ({out.posted_on.isoformat()})" for out in path)


def _onward_transfers(
    session: Session, everything: Mapping[int, TxnView], drafts: Sequence[Draft]
) -> dict[int, list[int]]:
    """For each outflow, the inflows its transfers and joint settlements carry its full amount to.

    A link counts when it exists; an automatic proposal counts as the link it becomes.
    """
    onward: dict[int, list[int]] = {}
    ids = list(everything)
    rows = session.execute(
        select(Link.from_transaction_id, Link.to_transaction_id, Link.amount_cents).where(
            Link.kind.in_(sorted(FORWARD_KINDS)),
            Link.from_transaction_id.in_(ids),
            Link.to_transaction_id.is_not(None),
        )
    )
    pairs: list[tuple[int, int, int]] = [(out, inn, cents) for out, inn, cents in rows]
    pairs.extend(
        (draft.out_txn_id, draft.in_txn_id, draft.amount_cents)
        for draft in drafts
        if draft.queue is Queue.auto_approve
        and draft.kind in FORWARD_KINDS
        and draft.out_txn_id is not None
        and draft.in_txn_id is not None
    )
    for out_id, in_id, cents in pairs:
        out = everything.get(out_id)
        if out is not None and cents == -out.amount_cents:
            onward.setdefault(out_id, []).append(in_id)
    return onward


def shared_words(first: str, second: str) -> tuple[str, ...]:
    """Words both descriptions use, in the first's order, leaving out the words every payment
    shares and the ones too short to mean anything."""
    in_second = set(_words(second))
    seen: dict[str, None] = {}
    for word in _words(first):
        if word in in_second and word not in GENERIC_WORDS and len(word) >= _MIN_WORD_LENGTH:
            seen.setdefault(word)
    return tuple(seen)


def _words(text: str) -> Iterable[str]:
    return re.findall(r"[a-z]+", text.lower())
