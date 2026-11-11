"""Joint-card settlement proposals anchored by reported bill payments."""

from collections.abc import Mapping
from dataclasses import dataclass

from omsync.ledger.links import LinkKind
from omsync.ledger.sources import AccountType
from omsync.reconcile.model import Draft, Queue, TxnView, pair_signature
from omsync.reconcile.pool import Pool
from omsync.reconcile.rules.base import DESCRIPTIONS, Emitter, claim
from omsync.reconcile.rules.transfers import CC_CREDIT_RE, ZELLE_RE, extract_zelle_conf

BILL_WINDOW_DAYS = 45
# Your outflow may leave any personal account that holds cash. Cards and brokerage do not qualify.
PERSONAL_OUTFLOW_TYPES = frozenset({AccountType.checking, AccountType.savings})
AUTO_CONFIDENCE = 0.90
OUTFLOW_ONLY_CONFIDENCE = 0.60
# How far a contribution may be from half the bill. A payment that names the joint card selects
# that card's bill by itself, so it can carry more slack than one matched by amount alone.
HALF_SLACK_CENTS = 1
NAMED_CARD_HALF_SLACK_CENTS = 100


@dataclass(frozen=True)
class _Fit:
    """How a contribution must match a bill: its slack from half, and the cards that may own it.

    cards is None when any joint card's bill may fit, and the named cards' IDs when the payment
    names them.
    """

    slack_cents: int = HALF_SLACK_CENTS
    cards: frozenset[int] | None = None

    @property
    def card_named(self) -> bool:
        return self.cards is not None


_TIGHT = _Fit()


def rule_joint_card_bill(pool: Pool, emit: Emitter) -> None:
    """For each joint card bill, find your outflow and your joint-checking inflow at half of it.

    Bills are evidence, so they come from every loaded transaction, linked or not: the bill's own
    payment may already be linked. A bill whose settlement is already approved gets no new pair,
    but its partner inflows still leave the pool.
    """
    anchors = [
        t
        for t in pool.evidence()
        if t.account_joint
        and t.account_type is AccountType.credit_card
        and t.amount_cents > 0
        and CC_CREDIT_RE.search(t.description)
    ]
    card_ids = frozenset(t.account_id for t in anchors)
    outflows = [t for t in pool if _is_outflow(t, emit)]
    fits = {t.id: _fit_for(emit, t, card_ids) for t in outflows}
    inflows = [
        t
        for t in pool
        if t.account_joint and t.account_type is AccountType.checking and t.amount_cents > 0
    ]
    loaded = {t.id: t for t in pool.evidence()}

    for bill in anchors:
        if bill.id in pool.settled_outflows:
            settled_outflow, fit = _settled_fit(
                emit, pool.settled_outflows[bill.id], loaded, card_ids
            )
            _take_partner_inflows(pool, emit, bill, anchors, inflows, settled_outflow, fit)
            continue
        mine = [
            (t, fits[t.id])
            for t in outflows
            if t.id in pool and _matches_bill(t, bill, anchors, fits[t.id])
        ]
        if len(mine) != 1:
            continue
        my_outflow, fit = mine[0]
        month = _month(bill)
        my_inflow = _own_inflow(pool, emit, my_outflow, bill, inflows, anchors, fit)
        if my_inflow is None:
            _propose_outflow_only(pool, emit, my_outflow, bill, month)
            continue

        partners = [
            t
            for t in inflows
            if t.id in pool
            and t.id != my_inflow.id
            and not _is_yours(emit, t, my_outflow)
            and _matches_bill(t, bill, anchors, fit)
        ]
        extra: dict[str, object] = {"bill_txn_id": bill.id, "bill_month": month}
        partner = partners[0] if len(partners) == 1 else None
        if partner is not None:
            extra["partner_contributions"] = [{"txn_id": partner.id}]
        elif partners:
            # Several possible partner inflows: do not guess which one is the contribution.
            extra["partner_candidates"] = [t.id for t in partners]
        if fit.card_named:
            extra["card_named"] = True

        emitted = claim(
            pool,
            emit,
            rule="joint_card_bill",
            kind=LinkKind.joint_settlement,
            first=my_outflow,
            second=my_inflow,
            queue=Queue.auto_approve,
            confidence=AUTO_CONFIDENCE,
            notes="payment names the joint card; amounts within $1.00 of half the bill"
            if fit.card_named
            else None,
            extra=extra,
        )
        if emitted:
            # Partner inflows are never linked: take every candidate out of the pool so no later
            # rule can propose one as a reimbursement.
            for candidate in partners:
                pool.take_partner(candidate.id)


def _is_outflow(txn: TxnView, emit: Emitter) -> bool:
    """True for your personal outflow that reads as a Zelle or confirmation-numbered transfer."""
    return (
        not txn.account_joint
        and txn.account_type in PERSONAL_OUTFLOW_TYPES
        and txn.amount_cents < 0
        and bool(
            emit.patterns.transfer_with_conf.search(txn.description)
            or ZELLE_RE.search(txn.description)
        )
    )


def _fit_for(emit: Emitter, outflow: TxnView, card_ids: frozenset[int]) -> _Fit:
    """The fit an outflow earns: loose and limited to the joint cards its description names."""
    named = (
        emit.accounts_by_digits(outflow.description) | emit.accounts_by_label(outflow.description)
    ) & card_ids
    return _Fit(NAMED_CARD_HALF_SLACK_CENTS, frozenset(named)) if named else _TIGHT


def _settled_fit(
    emit: Emitter,
    outflow_ids: tuple[int, ...],
    loaded: Mapping[int, TxnView],
    card_ids: frozenset[int],
) -> tuple[TxnView | None, _Fit]:
    """The approved payment of a bill already settled, and the fit it earns.

    The payment is the outflow the settlement links, read from the loaded transactions because it
    is linked. The fit is the loose one when that payment named the card; of several settlements
    of one bill, one that named the card wins. It is None when the payment is outside the range.
    """
    settled: tuple[TxnView | None, _Fit] = (None, _TIGHT)
    for outflow_id in outflow_ids:
        txn = loaded.get(outflow_id)
        if txn is None:
            continue
        fit = _fit_for(emit, txn, card_ids)
        if fit.card_named:
            return txn, fit
        if settled[0] is None:
            settled = (txn, fit)
    return settled


def _take_partner_inflows(
    pool: Pool,
    emit: Emitter,
    bill: TxnView,
    anchors: list[TxnView],
    inflows: list[TxnView],
    outflow: TxnView | None,
    fit: _Fit,
) -> None:
    """Take the partner inflows of a bill whose settlement is already approved, out of the pool.

    No pair is proposed here: the approved link already holds the partner's record.
    """
    for candidate in inflows:
        if (
            candidate.id in pool
            and not _is_yours(emit, candidate, outflow)
            and _matches_bill(candidate, bill, anchors, fit)
        ):
            pool.take_partner(candidate.id)


def _is_yours(emit: Emitter, inflow: TxnView, outflow: TxnView | None) -> bool:
    """True when a joint-checking inflow is your own contribution rather than your partner's.

    It is yours when it repeats the Zelle confirmation of your outflow, names one of your tracked
    personal accounts by its last four digits, or carries one of your own names. Any other
    inflow to joint checking is a partner contribution.
    """
    confirmation = extract_zelle_conf(outflow.description) if outflow is not None else None
    if confirmation and extract_zelle_conf(inflow.description) == confirmation:
        return True
    personal = {account.id for account in emit.accounts if not account.joint}
    if emit.accounts_by_digits(inflow.description, exclude=inflow.account_id) & personal:
        return True
    return emit.patterns.self_inflow.search(inflow.description) is not None


def _half_within_window(txn: TxnView, bill: TxnView, slack_cents: int) -> bool:
    """True when txn is half the bill to within the slack, posted within the window of the bill.

    |txn - bill / 2| <= slack, written in whole cents: |2 * txn - bill| <= 2 * slack.
    """
    half_fits = abs(2 * abs(txn.amount_cents) - bill.amount_cents) <= 2 * slack_cents
    return half_fits and abs((txn.posted_on - bill.posted_on).days) <= BILL_WINDOW_DAYS


def _matches_bill(txn: TxnView, bill: TxnView, anchors: list[TxnView], fit: _Fit) -> bool:
    """True when txn belongs to this bill: it fits the bill, and no other fitting bill is nearer.

    A payment that named its card competes only with that card's bills.
    """
    eligible = [b for b in anchors if fit.cards is None or b.account_id in fit.cards]
    if (fit.cards is not None and bill.account_id not in fit.cards) or not _half_within_window(
        txn, bill, fit.slack_cents
    ):
        return False
    fitting = [b for b in eligible if _half_within_window(txn, b, fit.slack_cents)]
    nearest = min(abs((txn.posted_on - b.posted_on).days) for b in fitting)
    return abs((txn.posted_on - bill.posted_on).days) == nearest


def _own_inflow(
    pool: Pool,
    emit: Emitter,
    outflow: TxnView,
    bill: TxnView,
    inflows: list[TxnView],
    anchors: list[TxnView],
    fit: _Fit,
) -> TxnView | None:
    """Your joint-checking inflow: the one with the same Zelle confirmation, else the only fit.

    Only inflows that are yours count (see _is_yours). With extra slack from half the bill, the
    inflow must also match the outflow to the cent, so the pair explains your payment and not a
    nearby amount.
    """
    candidates = [
        t
        for t in inflows
        if t.id in pool
        and _is_yours(emit, t, outflow)
        and _matches_bill(t, bill, anchors, fit)
        and (not fit.card_named or abs(t.amount_cents - abs(outflow.amount_cents)) <= 1)
    ]
    confirmation = extract_zelle_conf(outflow.description)
    if confirmation:
        same = [t for t in candidates if extract_zelle_conf(t.description) == confirmation]
        if len(same) == 1:
            return same[0]
    return candidates[0] if len(candidates) == 1 else None


def _propose_outflow_only(
    pool: Pool, emit: Emitter, outflow: TxnView, bill: TxnView, month: str
) -> None:
    """Your outflow with no joint inflow found: a manual proposal, nothing drained."""
    amount = abs(outflow.amount_cents)
    if emit.rejects(pair_signature(outflow.id, None, amount)):
        return
    emit.add(
        Draft(
            kind=LinkKind.joint_settlement,
            rule_key="joint_card_bill",
            out_txn_id=outflow.id,
            in_txn_id=None,
            amount_cents=amount,
            confidence=OUTFLOW_ONLY_CONFIDENCE,
            queue=Queue.manual,
            evidence={
                "rule": "joint_card_bill",
                "description": DESCRIPTIONS["joint_card_bill"],
                "conf_number": None,
                "date_gap_days": abs((outflow.posted_on - bill.posted_on).days),
                "amount_diff_cents": 0,
                "account_refs": {"out": None, "in": None, "check": "none"},
                "linked_cents": {"out": pool.linked_cents(outflow.id), "in": 0},
                "bill_txn_id": bill.id,
                "bill_month": month,
                "notes": "my_inflow_not_found",
            },
        )
    )


def _month(bill: TxnView) -> str:
    return f"{bill.posted_on.year}-{bill.posted_on.month:02d}"
