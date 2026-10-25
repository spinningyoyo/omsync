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

from omsync.ledger.counterparties import load_matchers
from omsync.ledger.links import RejectedProposal
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
from omsync.reconcile.patterns import build_patterns
from omsync.reconcile.pool import Pool, load_pool
from omsync.reconcile.rules.base import CONFIDENCE_BY_QUEUE, DESCRIPTIONS, Emitter
from omsync.reconcile.rules.counterparty import WINDOW_AFTER_DAYS, rule_counterparties
from omsync.reconcile.rules.joint import BILL_WINDOW_DAYS, rule_joint_settlement
from omsync.reconcile.rules.merchant_returns import (
    MERCHANT_RETURN_WINDOW_DAYS,
    rule_7a,
    rule_7b,
    rule_7c,
    rule_7d,
)
from omsync.reconcile.rules.p2p import ROUND_TRIP_WINDOW_DAYS, rule_round_trip
from omsync.reconcile.rules.reversals import (
    UNTRACKED_ROUND_TRIP_WINDOW_DAYS,
    rule_reversals,
    rule_untracked_round_trip,
)
from omsync.reconcile.rules.transfers import (
    rule_1a,
    rule_1b,
    rule_1c,
    rule_1d,
    rule_2,
    rule_2b,
    rule_3,
)
from omsync.reconcile.self_names import get_self_names


@dataclass(frozen=True)
class Rule:
    key: str
    run: Callable[[Pool, Emitter], None]
    description: str


RULES: tuple[Rule, ...] = (
    Rule("rv", rule_reversals, DESCRIPTIONS["rv"]),
    Rule("2a", rule_joint_settlement, DESCRIPTIONS["2a"]),
    Rule("1a", rule_1a, DESCRIPTIONS["1a"]),
    Rule("1b", rule_1b, DESCRIPTIONS["1b"]),
    Rule("1c", rule_1c, DESCRIPTIONS["1c"]),
    Rule("2", rule_2, DESCRIPTIONS["2"]),
    Rule("2b", rule_2b, DESCRIPTIONS["2b"]),
    Rule("1d", rule_1d, DESCRIPTIONS["1d"]),
    Rule("3", rule_3, DESCRIPTIONS["3"]),
    Rule("rt", rule_round_trip, DESCRIPTIONS["rt"]),
    Rule("ru", rule_untracked_round_trip, DESCRIPTIONS["ru"]),
    Rule("7a", rule_7a, DESCRIPTIONS["7a"]),
    Rule("7c", rule_7c, DESCRIPTIONS["7c"]),
    Rule("cp", rule_counterparties, DESCRIPTIONS["cp"]),
    Rule("7b", rule_7b, DESCRIPTIONS["7b"]),
    Rule("7d", rule_7d, DESCRIPTIONS["7d"]),
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
    WINDOW_AFTER_DAYS,
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
    its drafts say so. Payee patterns use
    your own names from the self_names preference, and named counterparties from the ledger.
    Descriptions that name an account are read against every tracked account, not only those with
    transactions in the range. A transaction no proposal involves, and that the transfer, card
    payment, and P2P wording says should have a counterpart, is Not matched unless you or a
    forward to a joint account explains it.

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
        patterns=build_patterns(get_self_names(session)),
        counterparties=load_matchers(session),
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
    not_matched, explained = assess(
        session, candidates=open_at_start, pool=pool, drafts=drafts, emit=emit, start=start, end=end
    )
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
