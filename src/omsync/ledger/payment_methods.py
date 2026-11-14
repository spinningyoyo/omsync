"""How money moved, read from a bank label: a transfer, a check, cash, a card, PayPal, and so on.

The reading follows Sure's Transaction::LabelNormalizer (we-promise/sure, AGPL-3.0, commit
0cb3284): a marker from bank_wording at the start of the label names the rail, and a payment
aggregator's prefix (PAYPAL *, SQ *, TST*) names who processed a card payment. Sure drops the
aggregator, because it is not who was paid; omsync keeps it, because it tells a cafe paid through
Square from a shop paid through PayPal when no payee keyword names either.

This reads wording only. Who was paid is the payee keywords' job, and they come first: see
categorize.
"""

import re
from collections.abc import Iterable
from enum import StrEnum
from functools import cache

from omsync.ledger.bank_wording import CONNECTORS, PACKS, RAIL_ORDER, Rx, Token


class PaymentMethod(StrEnum):
    """How money moved, as rules name it."""

    transfer = "transfer"  # bank transfers, Zelle, Venmo, ACH payments, and deposits
    check = "check"
    cash = "cash"
    card = "card"
    paypal = "paypal"
    square = "square"
    toast = "toast"
    fee = "fee"
    refund = "refund"
    loan_payment = "loan_payment"


METHOD_LABELS: dict[PaymentMethod, str] = {
    PaymentMethod.transfer: "Transfer",
    PaymentMethod.check: "Check",
    PaymentMethod.cash: "Cash",
    PaymentMethod.card: "Card",
    PaymentMethod.paypal: "PayPal",
    PaymentMethod.square: "Square",
    PaymentMethod.toast: "Toast",
    PaymentMethod.fee: "Fee",
    PaymentMethod.refund: "Refund",
    PaymentMethod.loan_payment: "Loan payment",
}

_RAIL_METHOD = {
    "transfer": PaymentMethod.transfer,
    "direct_debit": PaymentMethod.transfer,
    "cheque": PaymentMethod.check,
    "withdrawal": PaymentMethod.cash,
    "card": PaymentMethod.card,
    "fee": PaymentMethod.fee,
    "refund": PaymentMethod.refund,
    "loan_payment": PaymentMethod.loan_payment,
}

# US wording Sure's en_us pack lacks, found on the user's statements. Offer them back to Sure
# rather than letting the two drift.
OMSYNC_WORDING: dict[str, list[Token]] = {
    "transfer": [
        "ONLINE SCHEDULED TRANSFER",
        "ONLINE REALTIME TRANSFER",
        "REAL TIME TRANSFER",
        "MOBILE TRANSFER",
        "PAYPAL TRANSFER",
        "VENMO",
        "APPLE CASH",
        "MONEY NETWORK",
        "BKOFAMERICA MOBILE",
        "BKOFAMERICA BC",
        "REMOTE ONLINE DEPOSIT",
        "DEPOSIT ID NUMBER",
        "COUNTER CREDIT",
    ],
    "withdrawal": ["BKOFAMERICA ATM", "CA TLR CASH WITHDRAWAL", "WITHDRAWAL"],
}

# A US ACH line names the company first and its plumbing after ("CARDMEMBER SERV DES:WEB PYMT
# ID:..."). Sure reads the company as the payee and no rail; omsync reads the line as an ACH
# payment, a transfer, when no marker said otherwise.
_ACH_LINE = re.compile(r"\bDES\s*:.*\bID\s*:|\AORIG\s+CO\s+NAME\s*:", re.IGNORECASE)

# Payment aggregators put the real merchant after a star. The short codes are safe only because
# the star is required. (Sure's AGGREGATOR.)
_AGGREGATOR = re.compile(
    r"\A(PAYPAL|PP|SUMUP|SQC?|STRIPE|IZETTLE|ZETTLE|LYDIA|MOLLIE|ADYEN|GOCARDLESS|TST|CLV|WPY"
    r"|UBER|GOOGLE|EB|IC|SP|DD)\s*\*\s*",
    re.IGNORECASE,
)
_VIA_METHOD = {
    "PAYPAL": PaymentMethod.paypal,
    "PP": PaymentMethod.paypal,
    "SQ": PaymentMethod.square,
    "SQC": PaymentMethod.square,
    "TST": PaymentMethod.toast,
}

# A refund label usually nests the rail it reverses ("Refund Card Payment to ASOS").
_NESTED_RAILS = ("card", "direct_debit")

# A date or a terminal id may sit between the marker and an aggregator: "PURCHASE AUTHORIZED ON
# 09/02 SQ *CAFE". Only the first date is removed. (Sure's COMPACT_DATE, ALPHA_DATE, SLASHED_DATE,
# and TERMINAL_ID.)
_MONTHS = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
_DATES = (
    re.compile(r"\bDU\s+(\d{2})(\d{2})(\d{2})\b", re.IGNORECASE),
    re.compile(
        rf"\b(?:ON\s+)?(\d{{1,2}})[\s\-]?({_MONTHS})(?![A-Z])[\s\-]?(\d{{2,4}})?\b", re.IGNORECASE
    ),
    re.compile(r"\b(?:(?:DU|ON)\s+)?(\d{2})[/.\-](\d{2})(?:[/.\-](\d{2,4}))?\b", re.IGNORECASE),
)
_TERMINAL_ID = re.compile(r"\A\d{2,6}\b[\s:.,#\-]*")
_TRAILER = r"[\s:.,#\-]*(?:(?:" + "|".join(CONNECTORS) + r")\s+)?"


def payment_method(label: str) -> PaymentMethod | None:
    """How the money in a bank label moved, or None when its wording does not say.

    A marker at the start names the rail. With no marker, or a card one, an aggregator prefix
    names PayPal, Square, or Toast; another aggregator reads as a card. With neither, a US ACH
    line reads as a transfer.
    """
    working, rail = label, None
    for name, pattern in _markers():
        if pattern.search(working):
            working, rail = pattern.sub("", working, count=1), name
            break
    if rail == "refund":
        nested = [pattern for name, pattern in _markers() if name in _NESTED_RAILS]
        working = _remove_first(working, nested, "")
    working = _remove_first(working, _DATES, " ")
    working = working.lstrip()
    if rail is not None:
        working = _TERMINAL_ID.sub("", working, count=1)
    if rail is None or rail == "card":
        aggregator = _AGGREGATOR.match(working.lstrip())
        if aggregator is not None:
            return _VIA_METHOD.get(aggregator.group(1).upper(), PaymentMethod.card)
    if rail is None:
        return PaymentMethod.transfer if _ACH_LINE.search(label) else None
    return _RAIL_METHOD[rail]


def _remove_first(text: str, patterns: Iterable[re.Pattern[str]], replacement: str) -> str:
    """Text without the first match of the first pattern that matches."""
    for pattern in patterns:
        if pattern.search(text):
            return pattern.sub(replacement, text, count=1)
    return text


@cache
def _markers() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Every marker, compiled, by rail in RAIL_ORDER and, within a rail, most specific first."""
    return tuple((rail, _compile(token)) for rail in RAIL_ORDER for token in _tokens_for(rail))


def _tokens_for(rail: str) -> list[Token]:
    tokens = [token for pack in PACKS for token in pack["rails"].get(rail, [])]
    tokens += OMSYNC_WORDING.get(rail, [])
    unique = list(dict.fromkeys(tokens))
    return sorted(unique, key=lambda token: -len(token.source if isinstance(token, Rx) else token))


def _compile(token: Token) -> re.Pattern[str]:
    """A literal matches whole words with flexible whitespace, and ends on a word boundary only
    when it ends in a word character: one after "LSV+" would never match."""
    if isinstance(token, Rx):
        body = token.source
    else:
        body = r"\s+".join(re.escape(part) for part in token.split())
        if re.search(r"\w\Z", token):
            body += r"\b"
    return re.compile(rf"\A(?:{body}){_TRAILER}", re.IGNORECASE)


def parse_paid_by(stored: str | None) -> frozenset[PaymentMethod]:
    """The methods a rule's stored paid_by names. Raises ValueError for an unknown method."""
    return frozenset(PaymentMethod(value) for value in (stored or "").split())


def store_paid_by(methods: frozenset[PaymentMethod]) -> str | None:
    """The stored form of a rule's methods, in a fixed order, or None for none."""
    return " ".join(sorted(methods)) or None


def describe_methods(methods: frozenset[PaymentMethod]) -> str:
    """The methods as a reader names them, in the order PaymentMethod lists them: "Square or
    Toast"."""
    labels = [METHOD_LABELS[method] for method in PaymentMethod if method in methods]
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " or " + labels[-1]
