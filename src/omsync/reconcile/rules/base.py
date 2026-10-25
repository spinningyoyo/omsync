"""Shared proposal emission, candidate reservation, pairing, and evidence helpers."""

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from omsync.ledger.links import LinkKind
from omsync.reconcile.model import AccountRef, Draft, Queue, TxnView, pair_signature
from omsync.reconcile.patterns import DEFAULT_PATTERNS, Patterns
from omsync.reconcile.pool import Pool

DESCRIPTIONS: dict[str, str] = {
    "rv": "Reversal: Same-Account",
    "2a": "Joint: Card Bill Settlement",
    "1a": "Transfer: Conf# Hash Match",
    "1b": "Transfer: Zelle Conf# Match",
    "1c": "Transfer: Shared Transaction ID",
    "1d": "Transfer: Account Reference",
    "2": "Transfer: CC Bill Payment",
    "2b": "Transfer: CC Credit Balance Refund",
    "3": "Transfer: Generic (No Conf#)",
    "7a": "Merchant: Same-Account Return",
    "7c": "Merchant: Same-Account PayPal Return (±$0.01)",
    "rt": "P2P: Round Trip",
    "ru": "Return: Round Trip via Untracked Account",
    "7b": "Merchant: Cross-Account Return",
    "7d": "Merchant: Cross-Account Return (Brand Match)",
    # Proposed by earlier versions. Links made from those proposals keep these names.
    "cp": "P2P: Named Counterparty",
    "p2p": "P2P: General Reimbursement",
    "p2p_solo": "P2P: Solo Inflow (No Debit Match)",
}

CONFIDENCE_BY_QUEUE: dict[Queue, float] = {
    Queue.auto_approve: 1.0,
    Queue.review: 0.90,
    Queue.manual: 0.5,
}

# Ways a description names an account by its last four digits. A reference needs a marker, so a
# year or a store number is not one: a mask ("########4321", "CHK ...1111"), an account word
# ("ACCT# 2222", "CHK 3333", "account ending in 1234"), or "to"/"from" followed by up to three
# words ("to BOA EXAMPLE 4321"). Only the last form says which way the money went.
_MASKED_REF = re.compile(r"(?:[#*]{3,}|X{3,}|\.{3})\s*(\d{4})(?!\d)", re.IGNORECASE)
_LABELED_REF = re.compile(
    r"\b(?:ACCT|ACCOUNT|CHK|CHECKING|SAV|SAVINGS|CARD)\b\s*#?\s*(?:ENDING(?:\s+IN)?\s*)?(\d{4})(?!\d)",
    re.IGNORECASE,
)
_DIRECTIONAL_REF = re.compile(
    r"\b(to|from)\s+(?:[A-Za-z][\w&'.-]*\s+){0,3}?(?:[#*X.]{3,}\s*)?(\d{4})(?!\d)",
    re.IGNORECASE,
)


class Emitter:
    """The run's shared state: the drafts so far, rejected signatures, and matching config.

    Rules ask allowed() while they choose candidates. A rejected pair is therefore not counted
    as a candidate, so an ambiguity check sees only live options. patterns holds the payee
    patterns built from your own names.
    """

    def __init__(
        self,
        rejected: frozenset[str] | None = None,
        *,
        patterns: Patterns = DEFAULT_PATTERNS,
        accounts: Sequence[AccountRef] = (),
    ) -> None:
        self._rejected: frozenset[str] = rejected or frozenset()
        self.patterns = patterns
        self.accounts: tuple[AccountRef, ...] = tuple(accounts)
        self.drafts: list[Draft] = []

    def accounts_by_digits(self, text: str, *, exclude: int | None = None) -> set[int]:
        """Return the IDs of tracked accounts whose last four digits the text names.

        exclude is an account that cannot count, usually the transaction's own.
        """
        mentioned = account_mentions(text)
        if not mentioned:
            return set()
        return {a.id for a in self.accounts if a.id != exclude and a.digits & mentioned}

    def accounts_by_label(self, text: str) -> set[int]:
        """Return the IDs of tracked accounts whose name the text writes out.

        A one-word name is too generic to count. When one named account's name contains
        another's, only the longer name counts.
        """
        haystack = f" {_words(text)} "
        found: dict[int, str] = {}
        for account in self.accounts:
            for label in account.labels:
                words = _words(label)
                if " " in words and f" {words} " in haystack:
                    found[account.id] = max(found.get(account.id, ""), words, key=len)
        return {
            account_id
            for account_id, words in found.items()
            if not any(words != other and words in other for other in found.values())
        }

    def allowed(self, first: TxnView, second: TxnView) -> bool:
        """True unless this set of transactions and amount was rejected, whatever the kind."""
        return not self.rejects(pair_signature(first.id, second.id, pair_amount(first, second)))

    def rejects(self, key: str) -> bool:
        return key in self._rejected

    def add(self, draft: Draft) -> None:
        self.drafts.append(draft)


def pair_amount(first: TxnView, second: TxnView) -> int:
    """Return the amount a pair can explain: the smaller of the two absolute amounts.

    Exact matches are equal. A pair within one cent explains the smaller side, so neither side
    is over-explained.
    """
    return min(abs(first.amount_cents), abs(second.amount_cents))


def days_gap(first: TxnView, second: TxnView) -> int:
    """Return the number of days between two transactions, ignoring order."""
    return abs((first.posted_on - second.posted_on).days)


def days_after(later: TxnView, earlier: TxnView) -> int:
    """Return how many days later is after earlier. Negative when it comes first."""
    return (later.posted_on - earlier.posted_on).days


def within_one_cent(first: TxnView, second: TxnView) -> bool:
    return abs(abs(first.amount_cents) - abs(second.amount_cents)) <= 1


def claim(
    pool: Pool,
    emit: Emitter,
    *,
    rule: str,
    kind: LinkKind,
    first: TxnView,
    second: TxnView,
    queue: Queue,
    conf_number: str | None = None,
    notes: str | None = None,
    confidence: float | None = None,
    drain: bool | None = None,
    extra: Mapping[str, Any] | None = None,
) -> bool:
    """Propose a link between two transactions. Return False when the pair was rejected.

    The sides are ordered by sign: the money-out side is the debit. Both sides leave the pool
    unless the proposal is manual, unless drain says otherwise. confidence overrides the queue's
    value. extra is merged into the evidence.
    """
    out, inn = _order_sides(first, second)
    if not emit.allowed(out, inn):
        return False
    evidence: dict[str, Any] = {
        "rule": rule,
        "description": DESCRIPTIONS[rule],
        "conf_number": conf_number,
        "date_gap_days": days_gap(out, inn),
        "amount_diff_cents": abs(abs(out.amount_cents) - abs(inn.amount_cents)),
        "account_refs": account_refs(out, inn),
        "linked_cents": {"out": pool.linked_cents(out.id), "in": pool.linked_cents(inn.id)},
    }
    if extra:
        evidence.update(extra)
    if notes:
        evidence["notes"] = notes
    emit.add(
        Draft(
            kind=kind,
            rule_key=rule,
            out_txn_id=out.id,
            in_txn_id=inn.id,
            amount_cents=pair_amount(out, inn),
            confidence=CONFIDENCE_BY_QUEUE[queue] if confidence is None else confidence,
            queue=queue,
            evidence=evidence,
        )
    )
    should_drain = queue is not Queue.manual if drain is None else drain
    if should_drain:
        pool.take(out.id)
        pool.take(inn.id)
    return True


def pair_unambiguously(
    pool: Pool,
    emit: Emitter,
    *,
    rule: str,
    kind: LinkKind,
    debits: list[TxnView],
    credits: list[TxnView],
    fits: Callable[[TxnView, TxnView], bool],
    queue: Queue = Queue.auto_approve,
    extra: Mapping[str, Any] | None = None,
    prefer: Callable[[TxnView], bool] | None = None,
) -> None:
    """Pair each debit with its one credit, when neither side has another possible partner.

    A debit with several credits, or a credit with several debits, becomes a manual proposal of
    the first candidate. Manual proposals drain nothing, so the other candidates stay available.
    prefer settles a debit with several credits when exactly one of them passes it: that one is
    the debit's only candidate.
    """
    for debit in debits:
        if debit.id not in pool:
            continue
        candidates = [
            c for c in credits if c.id in pool and fits(debit, c) and emit.allowed(debit, c)
        ]
        notes = None
        if len(candidates) > 1 and prefer is not None:
            preferred = [c for c in candidates if prefer(c)]
            if len(preferred) == 1:
                notes = f"preferred over {len(candidates) - 1} other credit(s)"
                candidates = preferred
        if not candidates:
            continue
        if len(candidates) > 1:
            claim(
                pool,
                emit,
                rule=rule,
                kind=kind,
                first=debit,
                second=candidates[0],
                queue=Queue.manual,
                notes=f"ambiguous: {len(candidates)} credits match",
                extra=extra,
            )
            continue
        credit = candidates[0]
        partners = [
            d for d in debits if d.id in pool and fits(d, credit) and emit.allowed(d, credit)
        ]
        if len(partners) > 1:
            claim(
                pool,
                emit,
                rule=rule,
                kind=kind,
                first=debit,
                second=credit,
                queue=Queue.manual,
                notes=f"ambiguous: {len(partners)} debits match",
                extra=extra,
            )
            continue
        claim(
            pool,
            emit,
            rule=rule,
            kind=kind,
            first=debit,
            second=credit,
            queue=queue,
            notes=notes,
            extra=extra,
        )


def account_refs(out: TxnView, inn: TxnView) -> dict[str, Any]:
    """Describe the account each side names, and whether the names point at each other.

    The check is recorded as evidence. It does not change a queue. Only "to" and "from"
    references count. Values are "agrees" (both
    sides name the counterpart), "one_sided" (only one side names it), "contradicts" (a side
    names some other account), or "none".
    """
    out_ref = _directional_ref(out.description)
    in_ref = _directional_ref(inn.description)
    points: list[bool] = []
    if out_ref is not None and inn.account_last4 is not None:
        points.append(out_ref[1] == inn.account_last4)
    if in_ref is not None and out.account_last4 is not None:
        points.append(in_ref[1] == out.account_last4)

    if not points:
        check = "none"
    elif not all(points):
        check = "contradicts"
    elif len(points) == 2:
        check = "agrees"
    else:
        check = "one_sided"
    return {
        "out": _label(out_ref),
        "in": _label(in_ref),
        "check": check,
    }


def account_mentions(text: str) -> frozenset[str]:
    """Return the last-four digit groups a description uses to name an account."""
    return frozenset(
        match.group(match.lastindex)
        for pattern in (_MASKED_REF, _LABELED_REF, _DIRECTIONAL_REF)
        for match in pattern.finditer(text or "")
        if match.lastindex
    )


def _words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _directional_ref(text: str) -> tuple[str, str] | None:
    match = _DIRECTIONAL_REF.search(text or "")
    if match is None:
        return None
    return match.group(1).lower(), match.group(2)


def _label(ref: tuple[str, str] | None) -> str | None:
    return None if ref is None else f"{ref[0]} {ref[1]}"


def _order_sides(first: TxnView, second: TxnView) -> tuple[TxnView, TxnView]:
    if first.amount_cents < 0 < second.amount_cents:
        return first, second
    if second.amount_cents < 0 < first.amount_cents:
        return second, first
    raise ValueError("a link needs one money-out side and one money-in side")
