"""Confirmation-based transfers, card payments, and credit-balance refund proposals."""

import re
from collections import Counter, defaultdict
from datetime import date

from omsync.ledger.links import LinkKind
from omsync.ledger.sources import AccountType
from omsync.reconcile.model import Queue, TxnView
from omsync.reconcile.pool import Pool
from omsync.reconcile.rules.base import (
    Emitter,
    account_mentions,
    claim,
    days_gap,
    pair_unambiguously,
)
from omsync.reconcile.rules.merchant_returns import is_pipeline_merchant

# An autopay posts on the card on a Friday or Saturday and at the bank on Monday.
CC_PAYMENT_WINDOW_DAYS = 5
CC_CREDIT_BALANCE_WINDOW_DAYS = 7
SHARED_ID_WINDOW_DAYS = 3
ACCOUNT_REF_WINDOW_DAYS = 4

# Ten digits keep out dates, amounts, last fours, and nine-digit routing numbers. A statement
# NAME cut at its limit glues its MEMO on, so the digits may follow a letter directly.
_LONG_ID_RE = re.compile(r"(?<!\d)(\d{10,20})(?!\d)")

# Each pattern yields a confirmation token. "full" tokens are compared as written (lowercased).
# "suffix" and "numeric" tokens are compared by their last five digits, so a masked and an
# unmasked form of one transfer still match.
CONF_EXTRACTORS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Conf(?:irmation)?#\s*([a-z0-9]{7,})", re.I), "full"),
    (re.compile(r"Confirmation#\s*X{3,}(\d{5,})"), "suffix"),
    (re.compile(r"transaction\s*#?:\s*(\d{9,})"), "numeric"),
    (re.compile(r"ID:X{3,}(\d{5,})"), "suffix"),
    (re.compile(r"\bREF:\s*(\d{9,})"), "numeric"),
]

ZELLE_RE = re.compile(
    r"Zelle\s+(?:payment|Transfer|Recurring payment|Scheduled payment)"
    r".*?(?:Conf#\s*(\w{7,})|(?:BAC|JPM)([a-z0-9]{7,}))",
    re.IGNORECASE,
)

ZELLE_PREFIX_RE = re.compile(r"^\s*Zelle\b", re.IGNORECASE)

CC_DEBIT_RE = re.compile(
    r"CREDIT\s+CARDS?\s+Bill\s+Payment"
    r"|CREDIT\s+CRD\s+DES:AUTOPAY"
    r"|CARDMEMBER SERV DES:WEB PYMT"
    r"|\bELAN\s+DES:WEB\s+PYMT"
    r"|Online\s+Scheduled\s+Payment\s+to\s+ACCT#",
    re.IGNORECASE,
)

# The scheduled-payment wording is on both sides: the bank debit and, at some issuers, the card
# credit. The sign of the amount tells them apart.
CC_CREDIT_RE = re.compile(
    r"PAYMENT\s*-?\s*THANK\s*YOU"
    r"|Payment Thank You Bill Pa"
    r"|AUTOMATIC PAYMENT\s*-?\s*THANK"
    r"|PAYMENT\s+MADE\s+BY\s+ACCOUNT\s+ENDING"
    r"|PAYMENT\s+FROM\s+CHK"
    r"|ONLINE/MOBILE\s+RECURRING\s+FROM\s+CHK"
    r"|Online\s+Scheduled\s+Payment\s+to\s+ACCT#",
    re.IGNORECASE,
)

CB_DEBIT_RE = re.compile(
    r"ACH CREDIT BALANCE REF|CREDIT BALANCE REFUND",
    re.IGNORECASE,
)


# A bank mask: a run of three or more X, or an asterisk. A masked token can never equal its
# unmasked counterpart, so its unmasked suffix is used instead.
_MASK_RE = re.compile(r"X{3,}|\*", re.IGNORECASE)
_TRAILING_DIGITS_RE = re.compile(r"(\d{5,})$")


def extract_conf_key(payee: str) -> str | None:
    """Return the confirmation key of a transfer payee, or None when it has none.

    A full token is compared as written, lowercased. A masked full token falls back to the
    unmasked suffix: the suffix pattern if the payee has one, otherwise the digits at the token's
    end. That way "Confirmation# XXXXX12345" pairs with a counterpart that carries "12345".
    """
    for pattern, kind in CONF_EXTRACTORS:
        match = pattern.search(payee)
        if not match:
            continue
        value = match.group(1)
        if kind != "full":
            return value[-5:]
        if not _MASK_RE.search(value):
            return value.lower()
        return _unmasked_suffix(payee, value)
    return None


def _unmasked_suffix(payee: str, token: str) -> str | None:
    for pattern, kind in CONF_EXTRACTORS:
        if kind == "suffix":
            match = pattern.search(payee)
            if match:
                return match.group(1)[-5:]
    digits = _TRAILING_DIGITS_RE.search(token)
    return digits.group(1)[-5:] if digits else None


def extract_zelle_conf(text: str) -> str | None:
    """Return the lowercased Zelle confirmation token in a payee, or None."""
    match = ZELLE_RE.search(text)
    if match:
        return (match.group(1) or match.group(2)).lower()
    return None


def rule_transfer_same_confirmation(pool: Pool, emit: Emitter) -> None:
    """Transfer with the same confirmation number: same key, opposite signs, at most one day apart.

    When a key is shared by more than two transactions, only same-day, same-amount pairs of
    exactly two are paired. Any other group is left alone.
    """
    by_key: dict[str, list[TxnView]] = defaultdict(list)
    for txn in pool:
        if emit.patterns.transfer_with_conf.search(txn.description):
            key = extract_conf_key(txn.description)
            if key:
                by_key[key].append(txn)

    for key, txns in by_key.items():
        if len(txns) == 2:
            first, second = txns
            if days_gap(first, second) <= 1 and first.amount_cents + second.amount_cents == 0:
                claim(
                    pool,
                    emit,
                    rule="transfer_same_confirmation",
                    kind=LinkKind.transfer,
                    first=first,
                    second=second,
                    queue=Queue.auto_approve,
                    conf_number=key,
                )
        elif len(txns) > 2:
            by_day_and_amount: dict[tuple[date, int], list[TxnView]] = defaultdict(list)
            for txn in txns:
                by_day_and_amount[(txn.posted_on, abs(txn.amount_cents))].append(txn)
            for group in by_day_and_amount.values():
                if len(group) == 2 and group[0].amount_cents + group[1].amount_cents == 0:
                    claim(
                        pool,
                        emit,
                        rule="transfer_same_confirmation",
                        kind=LinkKind.transfer,
                        first=group[0],
                        second=group[1],
                        queue=Queue.auto_approve,
                        conf_number=key,
                        notes="suffix_collision_resolved",
                    )


def rule_zelle_same_confirmation(pool: Pool, emit: Emitter) -> None:
    """Zelle same confirmation number: same day, opposite signs, any pair of accounts.

    Three or more transactions with one token are skipped as ambiguous.
    """
    by_token: dict[str, list[TxnView]] = defaultdict(list)
    for txn in pool:
        token = extract_zelle_conf(txn.description)
        if token:
            by_token[token].append(txn)

    for token, txns in by_token.items():
        if len(txns) != 2:
            continue
        first, second = txns
        if days_gap(first, second) == 0 and first.amount_cents + second.amount_cents == 0:
            claim(
                pool,
                emit,
                rule="zelle_same_confirmation",
                kind=LinkKind.transfer,
                first=first,
                second=second,
                queue=Queue.auto_approve,
                conf_number=token,
            )


def rule_transfer_same_transaction_id(pool: Pool, emit: Emitter) -> None:
    """Shared transaction ID: one long number (or Zelle token) in exactly two transactions.

    The two must be on different accounts, equal and opposite, at most three days apart. An ID in
    any other number of transactions is skipped, and so is a transaction whose pair is not the
    only pair it has: either side with a second fitting partner means no ID decides it.
    """
    holders: dict[str, list[int]] = defaultdict(list)
    for txn in pool.evidence():
        for token in transfer_ids(txn.description):
            holders[token].append(txn.id)

    open_txns = {txn.id: txn for txn in pool}
    pairs: list[tuple[str, TxnView, TxnView]] = []
    for token, ids in holders.items():
        if len(ids) != 2 or not all(txn_id in open_txns for txn_id in ids):
            continue
        first, second = open_txns[ids[0]], open_txns[ids[1]]
        if _shared_id_fits(first, second) and emit.allowed(first, second):
            pairs.append((token, first, second))

    uses = Counter(txn.id for _, first, second in pairs for txn in (first, second))
    for token, first, second in pairs:
        if uses[first.id] == 1 and uses[second.id] == 1:
            claim(
                pool,
                emit,
                rule="transfer_same_transaction_id",
                kind=LinkKind.transfer,
                first=first,
                second=second,
                queue=Queue.auto_approve,
                conf_number=token,
            )


def transfer_ids(text: str) -> set[str]:
    """Return the identifiers a description carries: long numbers and Zelle confirmation tokens."""
    ids = set(_LONG_ID_RE.findall(text))
    zelle = extract_zelle_conf(text)
    if zelle:
        ids.add(zelle)
    return ids


def _shared_id_fits(first: TxnView, second: TxnView) -> bool:
    return (
        first.account_id != second.account_id
        and first.amount_cents + second.amount_cents == 0
        and days_gap(first, second) <= SHARED_ID_WINDOW_DAYS
        and not is_pipeline_merchant(first.payee_raw)
        and not is_pipeline_merchant(second.payee_raw)
    )


def rule_card_bill_payment(pool: Pool, emit: Emitter) -> None:
    """CC bill payment: a debit from a non-card account and a credit on a card, within 5 days."""
    debits, credits = _sides(pool, CC_DEBIT_RE, CC_CREDIT_RE)
    pair_unambiguously(
        pool,
        emit,
        rule="card_bill_payment",
        kind=LinkKind.transfer,
        debits=debits,
        credits=credits,
        fits=_cc_payment_fits,
    )


def rule_card_credit_refund(pool: Pool, emit: Emitter) -> None:
    """CC credit balance refund: a debit from a card, and a credit on a non-card account."""
    debits, credits = _sides(pool, CB_DEBIT_RE, emit.patterns.cb_credit)
    pair_unambiguously(
        pool,
        emit,
        rule="card_credit_refund",
        kind=LinkKind.transfer,
        debits=debits,
        credits=credits,
        fits=_credit_balance_fits,
    )


def rule_transfer_names_account(pool: Pool, emit: Emitter) -> None:
    """Account reference: a description names another tracked account that holds the counterparty.

    The named account must hold an equal, opposite amount within four days. Both directions must
    be unique, so two fitting partners leave a manual proposal. A side that names a different
    tracked account than its partner's does not fit. Of several fitting credits, the only one that
    reads as money from your own accounts is the debit's partner.
    """
    named = {
        txn.id: emit.accounts_by_digits(txn.description, exclude=txn.account_id) for txn in pool
    }
    targets = {account_id for names in named.values() for account_id in names}
    if not targets:
        return
    relevant = [txn for txn in pool if named[txn.id] or txn.account_id in targets]

    def fits(debit: TxnView, credit: TxnView) -> bool:
        if (
            abs(debit.amount_cents) != abs(credit.amount_cents)
            or debit.account_id == credit.account_id
            or days_gap(debit, credit) > ACCOUNT_REF_WINDOW_DAYS
        ):
            return False
        debit_names, credit_names = named[debit.id], named[credit.id]
        return (
            (credit.account_id in debit_names or debit.account_id in credit_names)
            and (not debit_names or credit.account_id in debit_names)
            and (not credit_names or debit.account_id in credit_names)
        )

    pair_unambiguously(
        pool,
        emit,
        rule="transfer_names_account",
        kind=LinkKind.transfer,
        debits=[txn for txn in relevant if txn.amount_cents < 0],
        credits=[txn for txn in relevant if txn.amount_cents > 0],
        fits=fits,
        prefer=lambda credit: is_own_inflow(credit, emit),
    )


def rule_transfer_same_amount(pool: Pool, emit: Emitter) -> None:
    """Generic transfer without a confirmation number: same day, same amount, another account.

    A credit that names a card payment pairs with the one debit that fits it, and only when that
    debit fits no other credit. The debit must move money between your own accounts (see
    is_transfer_like), so neither a purchase nor a Zelle payment to someone else pairs with a
    card-payment credit. A credit that names an account by its digits pairs only with a debit on
    that account: one naming an account omsync does not track, such as your partner's, pairs with
    nothing here. Anything more ambiguous is left alone.
    """
    credits = [
        t for t in pool if t.amount_cents > 0 and emit.patterns.generic.search(t.description)
    ]
    debits = [t for t in pool if t.amount_cents < 0 and is_transfer_like(t, emit)]

    def fits(debit: TxnView, credit: TxnView) -> bool:
        return (
            _generic_fits(debit, credit)
            and _paid_from_named_account(debit, credit, emit)
            and emit.allowed(debit, credit)
        )

    for credit in credits:
        if credit.id not in pool:
            continue
        candidates = [d for d in debits if d.id in pool and fits(d, credit)]
        if len(candidates) != 1:
            continue
        debit = candidates[0]
        partners = [c for c in credits if c.id in pool and fits(debit, c)]
        if len(partners) != 1:
            continue
        claim(
            pool,
            emit,
            rule="transfer_same_amount",
            kind=LinkKind.transfer,
            first=debit,
            second=credit,
            queue=Queue.auto_approve,
        )


def _sides(
    pool: Pool, debit_re: re.Pattern[str], credit_re: re.Pattern[str]
) -> tuple[list[TxnView], list[TxnView]]:
    """Split the pool into money-out and money-in candidates by payee text.

    The sign must agree with the side, so a pattern that appears on both sides, such as a
    scheduled-payment wording, puts each transaction on exactly one. Zelle descriptions are
    handled separately.
    """
    debits: list[TxnView] = []
    credits: list[TxnView] = []
    for txn in pool:
        if ZELLE_PREFIX_RE.search(txn.description):
            continue
        if txn.amount_cents < 0 and debit_re.search(txn.description):
            debits.append(txn)
        elif txn.amount_cents > 0 and credit_re.search(txn.description):
            credits.append(txn)
    return debits, credits


def _cc_payment_fits(debit: TxnView, credit: TxnView) -> bool:
    return (
        debit.account_id != credit.account_id
        and debit.account_type is not AccountType.credit_card
        and credit.account_type is AccountType.credit_card
        and abs(debit.amount_cents) == abs(credit.amount_cents)
        and days_gap(debit, credit) <= CC_PAYMENT_WINDOW_DAYS
    )


def _credit_balance_fits(debit: TxnView, credit: TxnView) -> bool:
    return (
        debit.account_id != credit.account_id
        and debit.account_type is AccountType.credit_card
        and credit.account_type is not AccountType.credit_card
        and abs(debit.amount_cents) == abs(credit.amount_cents)
        and days_gap(debit, credit) <= CC_CREDIT_BALANCE_WINDOW_DAYS
    )


def _paid_from_named_account(debit: TxnView, credit: TxnView, emit: Emitter) -> bool:
    """True unless the credit names an account by its digits and the debit is not on it."""
    if not account_mentions(credit.description):
        return True
    return debit.account_id in emit.accounts_by_digits(
        credit.description, exclude=credit.account_id
    )


def is_transfer_like(txn: TxnView, emit: Emitter) -> bool:
    """True when a debit moves money between your own accounts, so it is not a purchase.

    That is a transfer or payment wording, a Zelle payment to one of your own names, or a
    description that names another tracked account. A Zelle payment to someone else is not.
    """
    return bool(
        any(
            pattern.search(txn.description)
            for pattern in (
                emit.patterns.transfer_with_conf,
                emit.patterns.generic,
                emit.patterns.self_zelle,
                CC_DEBIT_RE,
                CB_DEBIT_RE,
            )
        )
        or emit.accounts_by_digits(txn.description, exclude=txn.account_id)
    )


def is_own_inflow(txn: TxnView, emit: Emitter) -> bool:
    """True when a credit reads as money from one of your own accounts.

    That is a card-payment credit, a transfer wording, a credit-balance refund, a Zelle payment
    from one of your own names, or a description that names another tracked account.
    """
    return bool(
        any(
            pattern.search(txn.description)
            for pattern in (
                CC_CREDIT_RE,
                emit.patterns.transfer_with_conf,
                emit.patterns.cb_credit,
                emit.patterns.generic,
                emit.patterns.self_inflow,
            )
        )
        or emit.accounts_by_digits(txn.description, exclude=txn.account_id)
    )


def _generic_fits(debit: TxnView, credit: TxnView) -> bool:
    return (
        debit.account_id != credit.account_id
        and debit.posted_on == credit.posted_on
        and abs(debit.amount_cents) == abs(credit.amount_cents)
    )
