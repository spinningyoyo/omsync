"""Transactions that should have a counterpart and do not, and the ones you need not look at.

A transaction is expected to have a counterpart when its description reads as a transfer, a
card payment, or a P2P payment from someone, or names another tracked account. It is Not matched
when no link covers all of it and no proposal of the run involves it. Reconciled stays what links
cover; nothing here changes it. Three things take a transaction out of Not matched without a
link, and each is shown as Explained with its reason: your acknowledgment of the transaction,
your standing acknowledgment of its counterparty, and a forward to a joint account (forwarded.py).
A partner's contribution that a joint settlement recorded is explained the same way.

assess reads the ledger and writes nothing.
"""

import itertools
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.ledger.counterparties import CounterpartyMatcher, load_matchers
from omsync.ledger.links import Acknowledgment
from omsync.ledger.transactions import Transaction
from omsync.reconcile.forwarded import find_forwards, is_p2p_inflow
from omsync.reconcile.model import Draft, Explained, ExplainedBy, NotMatched, TxnView
from omsync.reconcile.pool import Pool, transaction_description
from omsync.reconcile.rules.base import Emitter
from omsync.reconcile.rules.p2p import P2P_INFLOW_RE
from omsync.reconcile.rules.transfers import CC_CREDIT_RE, is_transfer_like

_NAME_WORD = re.compile(r"[A-Z][A-Za-z'.-]*$")
_FALLBACK_WORDS = 3
PARTNER_REASON = "Partner contribution to a joint card bill"


def counterpart_expected(txn: TxnView, emit: Emitter) -> bool:
    """True when the description reads as one half of a pair, so its other half should exist.

    Money out: the wording the P2P rule treats as a transfer between your own accounts (transfer
    and card-payment wordings, a Zelle payment to one of your own names, a named tracked account).
    Money in: a payment from someone by Zelle, Venmo, or PayPal; a card-payment credit; a transfer
    wording; or a named tracked account. A payment to someone else is not one half of anything in
    this ledger, so it is not expected to match.
    """
    if txn.amount_cents < 0:
        return is_transfer_like(txn, emit)
    text = txn.description
    return bool(
        any(
            pattern.search(text)
            for pattern in (
                P2P_INFLOW_RE,
                CC_CREDIT_RE,
                emit.patterns.transfer_with_conf,
                emit.patterns.cb_credit,
                emit.patterns.generic,
            )
        )
        or emit.accounts_by_digits(text, exclude=txn.account_id)
    )


def assess(
    session: Session,
    *,
    candidates: Sequence[TxnView],
    pool: Pool,
    drafts: Sequence[Draft],
    emit: Emitter,
    start: date,
    end: date,
) -> tuple[tuple[NotMatched, ...], tuple[Explained, ...]]:
    """Split the run's open transactions into Not matched and Explained.

    candidates are the transactions open when the run began, with the amount no link explains.
    The pool supplies the amounts links already cover and the recorded partner contributions.
    Rows come back in date order.
    """
    acknowledgments = {row.transaction_id: row for row in session.scalars(select(Acknowledgment))}
    drafted = {
        txn_id for draft in drafts for txn_id in (draft.out_txn_id, draft.in_txn_id) if txn_id
    }
    names = {account.id: account.labels[0] for account in emit.accounts}
    explained: list[Explained] = []
    undecided: list[TxnView] = []
    for txn in candidates:
        if txn.id in drafted:
            continue
        partner = txn.id in pool.partner_ids()
        acknowledgment = acknowledgments.get(txn.id)
        if acknowledgment is not None:
            explained.append(
                Explained(
                    txn=txn,
                    by=ExplainedBy.acknowledged,
                    reason=_acknowledged_reason(acknowledgment.note),
                    acknowledgment_id=acknowledgment.id,
                )
            )
            continue
        if partner:
            explained.append(Explained(txn=txn, by=ExplainedBy.partner, reason=PARTNER_REASON))
            continue
        if not counterpart_expected(txn, emit):
            continue
        matcher = _standing_acknowledgment(txn, emit.counterparties)
        if matcher is not None:
            explained.append(
                Explained(
                    txn=txn,
                    by=ExplainedBy.counterparty,
                    reason=f"No match expected from {matcher.name}",
                    counterparty_id=matcher.id,
                )
            )
            continue
        undecided.append(txn)

    explained.extend(
        Explained(txn=txn, by=ExplainedBy.partner, reason=PARTNER_REASON) for txn in pool.parked()
    )

    not_matched: list[NotMatched] = []
    forwardable = [txn for txn in undecided if is_p2p_inflow(txn) and not pool.linked_cents(txn.id)]
    outcomes = (
        find_forwards(session, forwardable, drafts=drafts, start=start, end=end)
        if forwardable
        else {}
    )
    for txn in undecided:
        outcome = outcomes.get(txn.id)
        if outcome is None or outcome.forward is None:
            not_matched.append(NotMatched(txn=txn, hint=None if outcome is None else outcome.hint))
            continue
        forward = outcome.forward
        last = forward.outflows[-1]
        via = "".join(f" via {names.get(account, 'another account')}" for account in forward.via)
        explained.append(
            Explained(
                txn=txn,
                by=ExplainedBy.forwarded,
                reason=(
                    f"Forwarded to {names.get(forward.arrival.account_id, 'a joint account')}"
                    f"{via} on {last.posted_on.isoformat()} (#{last.id})"
                ),
                forwarded_ids=tuple(out.id for out in forward.outflows),
                shared_words=forward.shared_words,
            )
        )

    return (
        tuple(sorted(not_matched, key=lambda row: (row.txn.posted_on, row.txn.id))),
        tuple(sorted(explained, key=lambda row: (row.txn.posted_on, row.txn.id))),
    )


@dataclass(frozen=True)
class CounterpartyChoices:
    """What you can pick to say no match is expected from a transaction's counterparty.

    matching are the saved counterparties whose patterns already cover the transaction, and
    suggested_pattern is a starting point for a new one.
    """

    description: str
    matching: tuple[CounterpartyMatcher, ...]
    suggested_pattern: str


def counterparty_choices(session: Session, txn: Transaction) -> CounterpartyChoices:
    description = transaction_description(session, txn)
    return CounterpartyChoices(
        description=description,
        matching=tuple(
            matcher
            for matcher in load_matchers(session)
            if matcher.matches(description, txn.amount_cents)
        ),
        suggested_pattern=suggest_pattern(description),
    )


def suggest_pattern(description: str) -> str:
    """A payee pattern that would match this description's counterparty, for you to edit.

    A payment from a person keeps the service wording and the capitalized words of the name that
    follows it ("Zelle payment from Steven Example"). Anything else keeps its leading words up to
    the first one with a digit in it, which is an identifier and not a name. The result is
    literal, so it matches only the words it was made from, whatever the spacing.
    """
    words = description.split()
    text = " ".join(words)
    match = P2P_INFLOW_RE.search(text)
    if match is not None and match.group(0).lower().endswith("from"):
        name = list(itertools.takewhile(_NAME_WORD.match, text[match.end() :].split()))
        if name:
            return _literal([*text[: match.end()].split(), *name])
    leading = list(itertools.takewhile(lambda word: not any(c.isdigit() for c in word), words))
    return _literal(leading[:_FALLBACK_WORDS] or words[:1])


def _literal(words: Sequence[str]) -> str:
    return r"\s+".join(re.escape(word) for word in words)


def _acknowledged_reason(note: str | None) -> str:
    return f"No match expected: {note}" if note else "No match expected"


def _standing_acknowledgment(
    txn: TxnView, matchers: Sequence[CounterpartyMatcher]
) -> CounterpartyMatcher | None:
    return next(
        (
            matcher
            for matcher in matchers
            if matcher.no_match_expected and matcher.matches(txn.description, txn.amount_cents)
        ),
        None,
    )
