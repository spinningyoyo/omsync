"""Presentation models for reconciliation proposals, receipt results, and link dialogs."""

import calendar
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from omsync.core import money
from omsync.core.months import parse_day
from omsync.core.settings import Settings
from omsync.ledger.links import Link, LinkKind
from omsync.ledger.sources import Account, account_digits
from omsync.ledger.transactions import Transaction
from omsync.receipts.service import ServiceReport, apply_digest, registered_vendors, run_vendor
from omsync.reconcile.decisions import coverage
from omsync.reconcile.engine import run
from omsync.reconcile.history import MANUAL_RULE, UNKNOWN_RULE, rule_tally
from omsync.reconcile.model import Draft, Explained, NotMatched, Queue, signature
from omsync.reconcile.rules.base import DESCRIPTIONS
from omsync.web.amounts import money_html
from omsync.web.contacts_view import ChoiceGroup, counterparty_choices

DEFAULT_LAST_DAYS = 90
CANDIDATE_LIMIT = 25
MANUAL_KIND_LABELS: dict[str, str] = {
    LinkKind.transfer.value: "Transfer",
    LinkKind.refund.value: "Merchant return",
    LinkKind.p2p.value: "P2P payment",
    LinkKind.joint_settlement.value: "Joint settlement",
}
KIND_LABELS: dict[str, str] = {**MANUAL_KIND_LABELS, LinkKind.receipt.value: "Receipt"}
QUEUE_LABELS: dict[str, str] = {
    Queue.auto_approve.value: "Auto",
    Queue.review.value: "Review",
    Queue.manual.value: "Manual",
}
QUEUE_TONES: dict[str, str] = {
    Queue.auto_approve.value: "green",
    Queue.review.value: "amber",
    Queue.manual.value: "gray",
}
COUNT_LABELS: dict[str, str] = {
    "matched": "Matched",
    "already_applied": "Already applied",
    "changed_since_applied": "Changed since applied",
    "ambiguous": "Ambiguous",
    "unmatched": "Unmatched",
    "insufficient": "Not enough data",
    "paid_elsewhere": "Paid elsewhere",
    "unknown_payer": "Unknown payer",
    "outside_period": "Matched outside this period",
    "will_apply": "Will apply",
    "apply_skipped": "Skipped",
}
# Each proposal group: the kind it shows, the rule keys it takes, and its heading.
_SECTION_TITLES: dict[str, str] = {
    "joint": "Joint settlement",
    "transfers": "Transfers",
    "returns": "Merchant returns",
    "p2p": "P2P payments",
}
_P2P_SUBTITLES: dict[str, str] = {"p2p_round_trip": "Round trip"}


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    start: date
    end: date


@dataclass(frozen=True)
class Side:
    """One money movement in a proposal, a link, or an unmatched row."""

    txn_id: int
    on: str
    account: str
    payee: str
    amount: str
    cents: int


@dataclass(frozen=True)
class ProposalRow:
    signature: str
    queue: str
    queue_label: str
    queue_tone: str
    confidence: str
    money_out: Side | None
    money_in: Side | None
    chips: tuple[str, ...]
    notes: tuple[str, ...]
    approve_key: str
    reject_key: str


@dataclass(frozen=True)
class ProposalGroup:
    """Rows that share a section and a heading. Its actions act on its own checked rows.

    Each action has its own idempotency key, made when the results render. A second click on the
    same button then applies once, and a different selection never replays an earlier batch.
    """

    key: str
    title: str
    rows: tuple[ProposalRow, ...]
    approve_key: str
    reject_key: str
    auto_key: str

    @property
    def auto_signatures(self) -> tuple[str, ...]:
        return tuple(row.signature for row in self.rows if row.queue == Queue.auto_approve.value)


@dataclass(frozen=True)
class VendorView:
    key: str
    title: str
    files: int
    receipts: int
    counts: tuple[tuple[str, int], ...]
    review: tuple[str, ...]
    to_apply: int
    apply_key: str
    apply_digest: str  # what the page shows: apply refuses if the operation now differs


@dataclass(frozen=True)
class NotMatchedRow:
    """A transaction expected to have a counterpart that nothing accounts for."""

    txn_id: int
    on: str
    account: str
    description: str
    amount: str
    category: str


@dataclass(frozen=True)
class ExplainedRow:
    """A transaction that needs nothing from you, with its counterparty as the reason."""

    txn_id: int
    on: str
    account: str
    description: str
    amount: str
    category: str
    reason: str
    by: str
    contact_id: int | None


@dataclass(frozen=True)
class LinkRow:
    link_id: int
    kind: str
    on: str
    money_out: Side | None
    money_in: Side | None
    amount: str
    rule: str
    manual: bool
    unlink_key: str


@dataclass(frozen=True)
class TallyRow:
    """What you did with one rule's output, over all history."""

    rule: str
    linked: int
    unlinked: int
    rejected: int
    paused: bool


@dataclass(frozen=True)
class Candidate:
    txn_id: int
    on: str
    account: str
    payee: str
    amount: str
    remaining: int
    remaining_text: str


@dataclass(frozen=True)
class Results:
    """What the results region shows for one range."""

    start: date
    end: date
    groups: tuple[ProposalGroup, ...]
    proposal_count: int
    queue_counts: tuple[tuple[str, int], ...]
    needs_you: int
    vendors: tuple[VendorView, ...]
    not_matched: tuple[NotMatchedRow, ...]
    explained: tuple[ExplainedRow, ...]
    counterparty_choices: tuple[ChoiceGroup, ...]
    linked: tuple[LinkRow, ...]
    tally: tuple[TallyRow, ...]


# Ranges


def quarter_of(day: date) -> tuple[date, date]:
    """Return the first and last day of the calendar quarter that holds day."""
    first_month = (day.month - 1) // 3 * 3 + 1
    last_month = first_month + 2
    last_day = calendar.monthrange(day.year, last_month)[1]
    return date(day.year, first_month, 1), date(day.year, last_month, last_day)


def presets(today: date) -> tuple[Preset, ...]:
    """The four presets, each computed from today."""
    this_start, this_end = quarter_of(today)
    last_start, last_end = quarter_of(this_start - timedelta(days=1))
    return (
        Preset("this-quarter", "This quarter", this_start, this_end),
        Preset("last-quarter", "Last quarter", last_start, last_end),
        Preset("last-90", "Last 90 days", today - timedelta(days=DEFAULT_LAST_DAYS - 1), today),
        Preset("ytd", "Year to date", date(today.year, 1, 1), today),
    )


def default_range(today: date) -> tuple[date, date]:
    """The range shown when the URL names none: the last 90 days, today included."""
    return today - timedelta(days=DEFAULT_LAST_DAYS - 1), today


def active_preset(today: date, start: date, end: date) -> str | None:
    for preset in presets(today):
        if (preset.start, preset.end) == (start, end):
            return preset.key
    return None


# Proposals


def build_groups(session: Session, drafts: Iterable[Draft]) -> tuple[ProposalGroup, ...]:
    """Group drafts by section, in the order the engine proposed them within each group."""
    drafts = list(drafts)
    sides = _sides(session, _txn_ids(drafts))
    buckets: dict[str, list[ProposalRow]] = {}
    titles: dict[str, str] = {}
    for draft in drafts:
        key, title = _group_of(draft)
        titles[key] = title
        buckets.setdefault(key, []).append(_row(draft, sides))
    order = ("joint", "transfers", "returns", "p2p-rt", "p2p-p2p")
    return tuple(
        ProposalGroup(
            key=key,
            title=titles[key],
            rows=tuple(buckets[key]),
            approve_key=uuid4().hex,
            reject_key=uuid4().hex,
            auto_key=uuid4().hex,
        )
        for key in order
        if key in buckets
    )


def queue_counts(drafts: Iterable[Draft]) -> tuple[tuple[str, int], ...]:
    counts = {queue.value: 0 for queue in Queue}
    for draft in drafts:
        counts[draft.queue.value] += 1
    return tuple((QUEUE_LABELS[key], counts[key]) for key in QUEUE_LABELS)


def _group_of(draft: Draft) -> tuple[str, str]:
    if draft.kind is LinkKind.joint_settlement:
        return "joint", _SECTION_TITLES["joint"]
    if draft.kind is LinkKind.transfer:
        return "transfers", _SECTION_TITLES["transfers"]
    if draft.kind is LinkKind.refund:
        return "returns", _SECTION_TITLES["returns"]
    sub = _P2P_SUBTITLES.get(draft.rule_key, "General")
    return f"p2p-{draft.rule_key}", f"{_SECTION_TITLES['p2p']}: {sub.lower()}"


def _row(draft: Draft, sides: dict[int, Side]) -> ProposalRow:
    chips, notes = _evidence(draft)
    return ProposalRow(
        signature=signature(draft),
        queue=draft.queue.value,
        queue_label=QUEUE_LABELS[draft.queue.value],
        queue_tone=QUEUE_TONES[draft.queue.value],
        confidence=f"{round(draft.confidence * 100)}%",
        money_out=sides.get(draft.out_txn_id) if draft.out_txn_id is not None else None,
        money_in=sides.get(draft.in_txn_id) if draft.in_txn_id is not None else None,
        chips=chips,
        notes=notes,
        approve_key=uuid4().hex,
        reject_key=uuid4().hex,
    )


def _evidence(draft: Draft) -> tuple[tuple[str, ...], tuple[str, ...]]:
    ev = draft.evidence
    chips: list[str] = []
    chips.append(DESCRIPTIONS.get(draft.rule_key, draft.rule_key))
    if ev.get("conf_number"):
        chips.append(f"Conf ending {str(ev['conf_number'])[-4:]}")
    gap = ev.get("date_gap_days")
    if gap is not None:
        chips.append("Same day" if gap == 0 else f"{gap} day{'s' if gap != 1 else ''} apart")
    diff = ev.get("amount_diff_cents")
    if diff:
        chips.append(f"{diff} cent{'s' if diff != 1 else ''} apart")
    check = (ev.get("account_refs") or {}).get("check")
    check_text = {
        "agrees": "Accounts agree",
        "one_sided": "One side names the other account",
        "contradicts": "Names a different account",
    }.get(check or "")
    if check_text:
        chips.append(check_text)
    if any((ev.get("linked_cents") or {}).values()):
        chips.append("Partly linked already")
    notes: list[str] = []
    if ev.get("notes"):
        notes.append(str(ev["notes"]))
    if ev.get("rule_unlinked"):
        notes.append(
            f"Not applied automatically: you removed {ev['rule_unlinked']} links this rule made"
        )
    for contribution in ev.get("partner_contributions", []):
        notes.append(f"Partner contribution: transaction #{contribution['txn_id']}")
    if ev.get("partner_candidates"):
        notes.append("Several partner inflows could be the contribution, so none is named")
    return tuple(chips), tuple(notes)


def _txn_ids(drafts: Iterable[Draft]) -> set[int]:
    ids: set[int] = set()
    for draft in drafts:
        for txn_id in (draft.out_txn_id, draft.in_txn_id):
            if txn_id is not None:
                ids.add(txn_id)
    return ids


def _sides(session: Session, txn_ids: set[int]) -> dict[int, Side]:
    if not txn_ids:
        return {}
    rows = session.execute(
        select(Transaction, Account)
        .join(Account, Account.id == Transaction.account_id)
        .where(Transaction.id.in_(txn_ids))
    ).all()
    return {txn.id: side_of(txn, account) for txn, account in rows}


def side_of(txn: Transaction, account: Account) -> Side:
    return Side(
        txn_id=txn.id,
        on=txn.posted_on.isoformat(),
        account=account_label(account.last4, account.alias_last4),
        payee=txn.payee,
        amount=money_html(txn.amount_cents),
        cents=txn.amount_cents,
    )


def account_label(last4: str | None, alias_last4: str | None = None) -> str:
    """The account's masked digits for display (see account_digits). None shows as a dash."""
    return account_digits(last4, alias_last4) or "—"


def plain_amount(cents: int) -> str:
    """Dollars and cents without a symbol, for an amount field: 2500 becomes 25.00."""
    sign = "-" if cents < 0 else ""
    dollars, remainder = divmod(abs(cents), 100)
    return f"{sign}{dollars}.{remainder:02d}"


# Receipts


def vendor_view(report: ServiceReport, title: str) -> VendorView:
    counts = tuple((COUNT_LABELS.get(key, key), report.counts.get(key, 0)) for key in COUNT_LABELS)
    review = tuple(f"{line.kind.replace('_', ' ')}: {line.text}" for line in report.review)
    return VendorView(
        key=report.vendor_key,
        title=title,
        files=report.files_read,
        receipts=report.receipts_read,
        counts=counts,
        review=review,
        to_apply=report.to_apply,
        apply_key=uuid4().hex,
        apply_digest=apply_digest(report),
    )


# Not matched and explained


def not_matched_rows(session: Session, found: Iterable[NotMatched]) -> list[NotMatchedRow]:
    found = list(found)
    aliases = _aliases(session)
    return [
        NotMatchedRow(
            txn_id=row.txn.id,
            on=row.txn.posted_on.isoformat(),
            account=account_label(row.txn.account_last4, aliases.get(row.txn.account_id)),
            description=row.txn.description,
            amount=money_html(row.txn.amount_cents),
            category=row.txn.category_name or "",
        )
        for row in found
    ]


def explained_rows(session: Session, found: Iterable[Explained]) -> list[ExplainedRow]:
    aliases = _aliases(session)
    return [
        ExplainedRow(
            txn_id=row.txn.id,
            on=row.txn.posted_on.isoformat(),
            account=account_label(row.txn.account_last4, aliases.get(row.txn.account_id)),
            description=row.txn.description,
            amount=money_html(row.txn.amount_cents),
            category=row.txn.category_name or "",
            reason=row.reason,
            by=row.by.value,
            contact_id=row.contact_id,
        )
        for row in found
    ]


def _aliases(session: Session) -> dict[int, str | None]:
    return dict(session.execute(select(Account.id, Account.alias_last4)).all())


# Linked


def linked_rows(session: Session, *, start: date, end: date) -> list[LinkRow]:
    in_range = select(Transaction.id).where(
        Transaction.posted_on >= start, Transaction.posted_on <= end
    )
    links = session.scalars(
        select(Link)
        .where(or_(Link.from_transaction_id.in_(in_range), Link.to_transaction_id.in_(in_range)))
        .order_by(Link.id)
    ).all()
    sides = _sides(
        session,
        {
            txn_id
            for link in links
            for txn_id in (link.from_transaction_id, link.to_transaction_id)
            if txn_id is not None
        },
    )
    rows: list[LinkRow] = []
    for link in links:
        out = sides.get(link.from_transaction_id) if link.from_transaction_id else None
        inn = sides.get(link.to_transaction_id) if link.to_transaction_id else None
        first = out or inn
        evidence = link.evidence or {}
        rule_key = str(evidence.get("rule", ""))
        manual = rule_key in ("", "manual")
        rows.append(
            LinkRow(
                link_id=link.id,
                kind=KIND_LABELS.get(link.kind.value, link.kind.value),
                on=first.on if first is not None else "",
                money_out=out,
                money_in=inn,
                amount=money_html(link.amount_cents),
                rule="Entered by hand" if manual else DESCRIPTIONS.get(rule_key, rule_key),
                manual=manual,
                unlink_key=uuid4().hex,
            )
        )
    return rows


def tally_rows(session: Session) -> list[TallyRow]:
    """Each rule's links, unlinks, and rejections, with the rule named as proposals name it."""
    return [
        TallyRow(
            rule=_rule_name(entry.rule),
            linked=entry.linked,
            unlinked=entry.unlinked,
            rejected=entry.rejected,
            paused=entry.paused,
        )
        for entry in rule_tally(session)
    ]


def _rule_name(rule: str) -> str:
    if rule == MANUAL_RULE:
        return "Entered by hand"
    if rule == UNKNOWN_RULE:
        return "Rejected before the rule was kept"
    return DESCRIPTIONS.get(rule, rule)


def link_candidates(
    session: Session,
    *,
    source_id: int,
    source_amount: int,
    query: str,
    start: date,
    end: date,
) -> list[Candidate]:
    """Transactions that could be the counterpart in a manual link.

    With no query, the candidates are the open transactions in the range of the opposite sign to
    the source: money in when the source is money out, and the reverse, closest in amount first.
    A search looks at every date and sign, newest first: a date (2026-08-01) finds that day, an
    amount finds that absolute amount, and anything else finds payees that contain the text. Only
    transactions with an open amount qualify, so a transaction a link already explains is never
    offered.
    """
    stmt = (
        select(Transaction, Account)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            Transaction.id != source_id,
            Transaction.amount_cents != 0,
            ~Transaction.splits.any(),
        )
    )
    text = query.strip()
    closest_first = ()
    if not text:
        opposite_sign = (
            Transaction.amount_cents > 0 if source_amount < 0 else Transaction.amount_cents < 0
        )
        stmt = stmt.where(
            opposite_sign, Transaction.posted_on >= start, Transaction.posted_on <= end
        )
        closest_first = (func.abs(func.abs(Transaction.amount_cents) - abs(source_amount)),)
    elif _is_iso_date(text):
        stmt = stmt.where(Transaction.posted_on == parse_day(text))
    elif _is_amount(text):
        stmt = stmt.where(func.abs(Transaction.amount_cents) == abs(money.to_cents(text)))
    else:
        stmt = stmt.where(
            or_(
                Transaction.payee.contains(text, autoescape=True),
                Transaction.payee_raw.contains(text, autoescape=True),
            )
        )
    newest_first = (Transaction.posted_on.desc(), Transaction.id.desc())
    rows = session.execute(stmt.order_by(*closest_first, *newest_first).limit(200)).all()
    candidates: list[Candidate] = []
    for txn, account in rows:
        remaining = abs(txn.amount_cents) - coverage(session, txn.id)
        if remaining <= 0:
            continue
        candidates.append(
            Candidate(
                txn_id=txn.id,
                on=txn.posted_on.isoformat(),
                account=account_label(account.last4, account.alias_last4),
                payee=txn.payee,
                amount=money_html(txn.amount_cents),
                remaining=remaining,
                remaining_text=money_html(remaining),
            )
        )
        if len(candidates) == CANDIDATE_LIMIT:
            break
    return candidates


def open_remaining(session: Session, txn_id: int) -> int:
    """The unlinked cents of a transaction, as a positive amount."""
    txn = session.get(Transaction, txn_id)
    if txn is None:
        raise LookupError(f"no transaction {txn_id}")
    return abs(txn.amount_cents) - coverage(session, txn_id)


def _is_iso_date(text: str) -> bool:
    try:
        parse_day(text)
    except ValueError:
        return False
    return len(text) == 10


def _is_amount(text: str) -> bool:
    try:
        money.to_cents(text)
    except (ValueError, TypeError):
        return False
    return True


# The whole results region


def results_for(
    session: Session, settings: Settings, *, start: date, end: date, today: date
) -> Results:
    """Run every proposal rule and each receipt vendor for one range. Writes nothing."""
    result = run(session, start=start, end=end)
    drafts = list(result.drafts)
    vendors = tuple(
        vendor_view(
            run_vendor(session, settings, vendor.key, today=today, start=start, end=end),
            vendor.key.title(),
        )
        for vendor in registered_vendors()
    )
    return Results(
        start=start,
        end=end,
        groups=build_groups(session, drafts),
        proposal_count=len(drafts),
        queue_counts=queue_counts(drafts),
        needs_you=result.needs_you,
        vendors=vendors,
        not_matched=tuple(not_matched_rows(session, result.not_matched)),
        explained=tuple(explained_rows(session, result.explained)),
        counterparty_choices=counterparty_choices(session),
        linked=tuple(linked_rows(session, start=start, end=end)),
        tally=tuple(tally_rows(session)),
    )
