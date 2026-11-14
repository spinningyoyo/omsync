"""The reconciliation run: the rules in order over one date range, returning proposals.

run() reads the ledger and returns drafts, and with them the transactions that are Not matched or
Explained (see not_matched.py). It writes nothing: no proposal rows, no batch, and no data
revision. Saving proposals is a later step (see decisions.py). Rules run in RULES order, and each
one drains the pool for the rules after it.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.ledger.links import RejectedProposal
from omsync.ledger.own_names import own_name_literals
from omsync.ledger.patterns import build_patterns
from omsync.ledger.sources import Account, Institution
from omsync.reconcile.history import demoted_rules
from omsync.reconcile.model import (
    AccountRef,
    Draft,
    Explained,
    NotMatched,
    Queue,
    pair_signature,
)
from omsync.reconcile.not_matched import assess
from omsync.reconcile.pool import Pool, load_pool
from omsync.reconcile.rules.base import CONFIDENCE_BY_QUEUE, DESCRIPTIONS, Emitter
from omsync.reconcile.rules.joint import BILL_WINDOW_DAYS, rule_joint_card_bill
from omsync.reconcile.rules.merchant_returns import (
    MERCHANT_RETURN_WINDOW_DAYS,
    rule_paypal_return_same_account,
    rule_return_other_account_same_store,
    rule_return_same_account,
)
from omsync.reconcile.rules.p2p import ROUND_TRIP_WINDOW_DAYS, rule_p2p_round_trip
from omsync.reconcile.rules.reversals import (
    UNTRACKED_ROUND_TRIP_WINDOW_DAYS,
    rule_return_via_untracked_account,
    rule_reversal_same_account,
)
from omsync.reconcile.rules.transfers import (
    rule_card_bill_payment,
    rule_card_credit_refund,
    rule_transfer_names_account,
    rule_transfer_same_amount,
    rule_transfer_same_confirmation,
    rule_transfer_same_transaction_id,
    rule_zelle_same_confirmation,
)


@dataclass(frozen=True)
class Rule:
    key: str
    run: Callable[[Pool, Emitter], None]
    description: str


RULES: tuple[Rule, ...] = (
    Rule(
        "reversal_same_account", rule_reversal_same_account, DESCRIPTIONS["reversal_same_account"]
    ),
    Rule("joint_card_bill", rule_joint_card_bill, DESCRIPTIONS["joint_card_bill"]),
    Rule(
        "transfer_same_confirmation",
        rule_transfer_same_confirmation,
        DESCRIPTIONS["transfer_same_confirmation"],
    ),
    Rule(
        "zelle_same_confirmation",
        rule_zelle_same_confirmation,
        DESCRIPTIONS["zelle_same_confirmation"],
    ),
    Rule(
        "transfer_same_transaction_id",
        rule_transfer_same_transaction_id,
        DESCRIPTIONS["transfer_same_transaction_id"],
    ),
    Rule("card_bill_payment", rule_card_bill_payment, DESCRIPTIONS["card_bill_payment"]),
    Rule("card_credit_refund", rule_card_credit_refund, DESCRIPTIONS["card_credit_refund"]),
    Rule(
        "transfer_names_account",
        rule_transfer_names_account,
        DESCRIPTIONS["transfer_names_account"],
    ),
    Rule("transfer_same_amount", rule_transfer_same_amount, DESCRIPTIONS["transfer_same_amount"]),
    Rule("p2p_round_trip", rule_p2p_round_trip, DESCRIPTIONS["p2p_round_trip"]),
    Rule(
        "return_via_untracked_account",
        rule_return_via_untracked_account,
        DESCRIPTIONS["return_via_untracked_account"],
    ),
    Rule("return_same_account", rule_return_same_account, DESCRIPTIONS["return_same_account"]),
    Rule(
        "paypal_return_same_account",
        rule_paypal_return_same_account,
        DESCRIPTIONS["paypal_return_same_account"],
    ),
    Rule(
        "return_other_account_same_store",
        rule_return_other_account_same_store,
        DESCRIPTIONS["return_other_account_same_store"],
    ),
)

# The farthest any rule looks from a transaction for its partner or a competing candidate. A run
# reads this much beyond each end of its range, so a transaction near an edge meets the same
# candidates as in a run over the whole history, and a match is never certain only because its
# rival posted outside the range.
CONTEXT_DAYS = max(
    BILL_WINDOW_DAYS,
    ROUND_TRIP_WINDOW_DAYS,
    UNTRACKED_ROUND_TRIP_WINDOW_DAYS,
    MERCHANT_RETURN_WINDOW_DAYS,
)


@dataclass(frozen=True)
class RuleStats:
    rule: str
    proposed: int
    drained: int


@dataclass(frozen=True)
class RunResult:
    run_id: str
    start: date
    end: date
    drafts: tuple[Draft, ...]
    stats: tuple[RuleStats, ...]
    not_matched: tuple[NotMatched, ...] = ()
    explained: tuple[Explained, ...] = ()

    @property
    def needs_you(self) -> int:
        """What waits for your decision: review and manual proposals, and Not matched rows.

        Automatic proposals are applied after each import without you, and Explained rows need
        nothing.
        """
        waiting = sum(1 for draft in self.drafts if draft.queue is not Queue.auto_approve)
        return waiting + len(self.not_matched)


def run(
    session: Session,
    *,
    start: date,
    end: date,
    include_linked: bool = False,
) -> RunResult:
    """Propose links for transactions posted from start to end, inclusive. Writes nothing.

    Rejected pairs are skipped, so they neither appear nor block a candidate. A rule whose links
    you removed twice (see history.py) proposes in the review queue, never the automatic one, and
    its drafts say so. Payee patterns use the owner's names and your accounts' nicknames.
    Descriptions that name an account are read against every tracked account, not only those with
    transactions in the range. A transaction no proposal involves, and that the transfer, card
    payment, and P2P wording says should have a counterpart, is Not matched unless its counterparty
    is known (see not_matched.py).

    The rules read CONTEXT_DAYS beyond each end of the range, and the result keeps the proposals
    with a side posted in the range, and the Not matched and Explained rows posted in it.
    """
    context = timedelta(days=CONTEXT_DAYS)
    pool = load_pool(
        session, start=start - context, end=end + context, include_linked=include_linked
    )
    posted = {txn.id: txn.posted_on for txn in pool.evidence()}

    def in_range(txn_id: int | None) -> bool:
        return txn_id is not None and start <= posted[txn_id] <= end

    rejected = frozenset(session.scalars(select(RejectedProposal.signature)))
    emit = Emitter(
        rejected,
        patterns=build_patterns(own_name_literals(session)),
        accounts=_account_refs(session),
    )

    open_at_start = tuple(txn for txn in pool if in_range(txn.id))
    stats: list[RuleStats] = []
    for rule in RULES:
        pool_before = len(pool)
        drafts_before = len(emit.drafts)
        rule.run(pool, emit)
        stats.append(
            RuleStats(
                rule=rule.key,
                proposed=len(emit.drafts) - drafts_before,
                drained=pool_before - len(pool),
            )
        )

    # The emitter already refuses rejected pairs as candidates. This final check makes the
    # guarantee hold for every draft, whatever rule produced it.
    demoted = demoted_rules(session)
    drafts = tuple(
        _demote(draft, demoted[draft.rule_key])
        if draft.queue is Queue.auto_approve and draft.rule_key in demoted
        else draft
        for draft in emit.drafts
        if pair_signature(draft.out_txn_id, draft.in_txn_id, draft.amount_cents) not in rejected
    )
    candidates = (*open_at_start, *(txn for txn in pool.parked() if in_range(txn.id)))
    not_matched, explained = assess(session, candidates=candidates, drafts=drafts, emit=emit)
    return RunResult(
        run_id=str(uuid4()),
        start=start,
        end=end,
        drafts=tuple(d for d in drafts if in_range(d.out_txn_id) or in_range(d.in_txn_id)),
        stats=tuple(stats),
        not_matched=not_matched,
        explained=tuple(row for row in explained if in_range(row.txn.id)),
    )


def _demote(draft: Draft, unlinked: int) -> Draft:
    """Move an automatic draft to review, with the count of links you removed from its rule."""
    return replace(
        draft,
        queue=Queue.review,
        confidence=CONFIDENCE_BY_QUEUE[Queue.review],
        evidence={**draft.evidence, "rule_unlinked": unlinked},
    )


def _account_refs(session: Session) -> list[AccountRef]:
    rows = session.execute(
        select(
            Account.id,
            Account.name,
            Institution.name,
            Account.last4,
            Account.alias_last4,
            Account.joint,
        )
        .join(Institution, Institution.id == Account.institution_id)
        .order_by(Account.id)
    ).all()
    return [
        AccountRef(
            id=account_id,
            labels=(name, f"{institution} {name}"),
            digits=frozenset(d for d in (last4, alias_last4) if d),
            joint=joint,
        )
        for account_id, name, institution, last4, alias_last4, joint in rows
    ]
