"""P2P round trips, and the wordings of payments to and from people."""

import re
from dataclasses import replace

from omsync.ledger.links import LinkKind
from omsync.reconcile.model import Queue, TxnView
from omsync.reconcile.pool import Pool
from omsync.reconcile.rules.base import Emitter, claim, days_gap
from omsync.reconcile.rules.transfers import is_transfer_like

# Zelle, Venmo, and PayPal credits that say they came from someone.
P2P_INFLOW_RE = re.compile(
    r"Zelle\s+(?:payment|Transfer|Recurring payment|Scheduled payment)\s+from"
    r"|VENMO\s+(?:DES:)?CASHOUT"
    r"|Venmo\s+(?:payment|transfer)\s+from"
    r"|PAYPAL\s+(?:TRANSFER|INST\s*XFER|DES:)"
    r"|PayPal\s+(?:transfer|payment)\s+from",
    re.IGNORECASE,
)

_ZELLE_PERSON_RES = {
    direction: re.compile(
        r"Zelle\s+(?:payment|Transfer|Recurring payment|Scheduled payment)\s+"
        rf"{direction}\s+(?P<rest>.*)",
        re.IGNORECASE,
    )
    for direction in ("to", "from")
}
_NAME_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'.-]*")
_MEMO_RE = re.compile(r"\s+for\b", re.IGNORECASE)
# Wordings that put the person after the confirmation: "Zelle Transfer Conf# ABC123; NAME" and
# Bank of America's "Online Banking Transfer Conf# ABC123; NAME" for a transfer from one of its
# other customers, which exists for money in only.
_AFTER_CONFIRMATION_RE = re.compile(
    r"^\s*(?:Zelle\b[^;]*?|(?P<bank>Online Banking Transfer))\s*\bConf#\s*\w+\s*;\s*(?P<rest>.+)$",
    re.IGNORECASE,
)
_NAME_END_RE = re.compile(r"\s+for\b|;", re.IGNORECASE)

ROUND_TRIP_WINDOW_DAYS = 30


def rule_p2p_round_trip(pool: Pool, emit: Emitter) -> None:
    """Round trip: a Zelle payment to a person and the same amount from them, in either order.

    The first two name tokens must agree, where a one-letter second token matches a second token
    that starts with it. The amounts are equal and the dates at most 30 days apart. Every pair is
    automatic: payments to one person of one amount are interchangeable, so of several, the
    nearest pairs are linked first, by date gap and then IDs. Your own names, accounts you name,
    payments whose counterparty is stored as the owner or the co-owner, and anything an earlier rule
    proposed are left out. The memo after "for" never decides a match.
    """
    proposed = {t for d in emit.drafts for t in (d.out_txn_id, d.in_txn_id)}
    people = {t.id: _person(t, emit) for t in pool if t.id not in proposed}
    outflows = [t for t in pool if people.get(t.id) and t.amount_cents < 0]
    inflows = [t for t in pool if people.get(t.id) and t.amount_cents > 0]

    def fits(out: TxnView, inn: TxnView) -> bool:
        return (
            _same_person(people[out.id], people[inn.id])
            and abs(out.amount_cents) == inn.amount_cents
            and days_gap(out, inn) <= ROUND_TRIP_WINDOW_DAYS
            and emit.allowed(out, inn)
        )

    partners = {out.id: [i for i in inflows if fits(out, i)] for out in outflows}
    senders = {i.id: [o for o in outflows if fits(o, i)] for i in inflows}
    pairs = sorted(
        ((out, inn) for out in outflows for inn in partners[out.id]),
        key=lambda pair: (days_gap(*pair), pair[0].id, pair[1].id),
    )
    for out, inn in pairs:
        if out.id not in pool or inn.id not in pool:
            continue
        unique = len(partners[out.id]) == 1 and len(senders[inn.id]) == 1
        claim(
            pool,
            emit,
            rule="p2p_round_trip",
            kind=LinkKind.p2p,
            first=out,
            second=inn,
            queue=Queue.auto_approve,
            notes=None
            if unique
            else f"nearest of {len(partners[out.id])} inflows, {len(senders[inn.id])} outflows",
            extra={"person": " ".join(people[out.id] or ())},
        )


def payment_name_text(description: str, amount_cents: int) -> str | None:
    """The part of a payment's description that names the other person, or None when it is not
    such a payment.

    A Zelle payment to someone has money out and one from someone has money in. The person follows
    "to" or "from", or in the older wordings follows the confirmation after a semicolon, in a
    Zelle Transfer or a Bank of America online transfer from another customer. The text stops
    before a memo ("for ...") or a semicolon, so a memo is never read as a name.
    """
    direction = "to" if amount_cents < 0 else "from"
    match = _ZELLE_PERSON_RES[direction].search(description)
    rest = match.group("rest") if match is not None else None
    if rest is None:
        older = _AFTER_CONFIRMATION_RE.match(description)
        if older is not None and (amount_cents > 0 or older.group("bank") is None):
            rest = older.group("rest")
    if rest is None:
        return None
    return _NAME_END_RE.split(rest, maxsplit=1)[0].strip() or None


def written_name(name_text: str) -> str | None:
    """The person as the description writes them: the leading words of the name text, up to the
    first that is not a name word, or None when it has none."""
    words: list[str] = []
    for word in name_text.split():
        if not _NAME_TOKEN_RE.fullmatch(word) or word.lower() == "conf":
            break
        words.append(word)
    return " ".join(words) or None


def _person(txn: TxnView, emit: Emitter) -> tuple[str, str] | None:
    """Return the first two name tokens of a Zelle payment to or from someone, lowercased.

    None when the description is not such a payment, names fewer than two tokens, or is your own
    money (your name, a tracked account, or a payment whose stored counterparty is the owner or the
    co-owner of the joint accounts).
    """
    direction = "to" if txn.amount_cents < 0 else "from"
    match = _ZELLE_PERSON_RES[direction].search(txn.description)
    if match is None:
        return None
    tokens: list[str] = []
    for word in match.group("rest").split():
        if not _NAME_TOKEN_RE.fullmatch(word) or word.lower() in ("for", "conf"):
            break
        tokens.append(word.lower())
        if len(tokens) == 2:
            break
    if len(tokens) < 2:
        return None
    # The memo after "for" must not decide whether the payment is your own money.
    core = replace(txn, description=_MEMO_RE.split(txn.description, maxsplit=1)[0])
    own = (
        is_transfer_like(core, emit)
        if txn.amount_cents < 0
        else emit.patterns.self_inflow.search(core.description)
        or emit.accounts_by_digits(core.description, exclude=txn.account_id)
    )
    return None if own or txn.with_household else (tokens[0], tokens[1])


def _same_person(first: tuple[str, str] | None, second: tuple[str, str] | None) -> bool:
    """True when the first tokens agree and the second tokens agree or one is an initial."""
    if first is None or second is None or first[0] != second[0]:
        return False
    short, long = sorted((first[1], second[1]), key=len)
    return short == long or (len(short) == 1 and long.startswith(short))
