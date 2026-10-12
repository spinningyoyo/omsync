"""General P2P reimbursement proposals and evidence scoring."""

import math
import re

from omsync.ledger.links import LinkKind
from omsync.receipts.vendors import BUILT_IN_VENDORS
from omsync.reconcile.model import Queue, TxnView
from omsync.reconcile.pool import Pool
from omsync.reconcile.rules.base import Emitter, claim, days_after
from omsync.reconcile.rules.merchant_returns import MERCHANT_EXCLUDED_CATEGORIES
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

P2P_EXCLUDED_CATEGORIES = MERCHANT_EXCLUDED_CATEGORIES

STRONG_EVIDENCE_KEYWORDS = frozenset(
    {
        "costco",
        "home depot",
        "homedepot",
        "target",
        "walmart",
        "amazon",
        "lowes",
        "best buy",
        "bestbuy",
        "ikea",
        "trader joe",
        "whole foods",
        "safeway",
        "kroger",
        "publix",
        "cvs",
        "walgreens",
        "rite aid",
        "dicks sporting",
        "dicksportinggoods",
        "rei",
        "nordstrom",
        "macys",
    }
)

# Words every P2P description shares, which say how the money moved and not what it was for.
GENERIC_WORDS = frozenset(
    {
        "zelle",
        "paypal",
        "venmo",
        "cash",
        "cashapp",
        "apple",
        "pay",
        "payment",
        "pymt",
        "transfer",
        "xfer",
        "instant",
        "recurring",
        "scheduled",
        "online",
        "mobile",
        "from",
        "to",
        "for",
        "the",
        "and",
        "with",
        "conf",
        "confirmation",
        "cashout",
        "des",
        "web",
        "ach",
    }
)

WEAK_EVIDENCE_KEYWORDS = frozenset(
    {
        "groceries",
        "grocery",
        "food",
        "gas",
        "dinner",
        "lunch",
        "drinks",
        "shopping",
        "store",
        "supplies",
        "stuff",
        "trip",
        "travel",
    }
)

BASE_P2P_CONFIDENCE = 0.60
P2P_CONFIDENCE_CAP = 0.90
AMOUNT_BOOST_THRESHOLD = 100.0  # dollars
AMOUNT_BOOST_MULTIPLIER = 0.088  # 0.15 / (log10(5000) - log10(100))
REVIEW_THRESHOLD = 0.90
WINDOW_DAYS = 30

_AMAZON = next(vendor for vendor in BUILT_IN_VENDORS if vendor.key == "amazon")


def rounding_penalty(debit: TxnView, credit: TxnView) -> float:
    """-0.05 when the two amounts differ by one cent, else nothing."""
    diff = abs(abs(debit.amount_cents) - abs(credit.amount_cents))
    return -0.05 if 0 < diff <= 1 else 0.0


def evidence_boost(memo: str, debit_payee: str) -> float:
    """Relationship evidence between the inflow's text and the debit's payee: 0.15, 0.07, or 0.

    Keywords match whole words, so "rei" is not found in "reimbursement". Words every P2P
    description shares do not count as the payee's name.
    """
    memo_words = _words(memo)

    # Strong: Amazon on both sides, under any of its spellings.
    if _AMAZON.is_vendor_payee(memo or "") and _AMAZON.is_vendor_payee(debit_payee or ""):
        return 0.15

    # Strong: a specific merchant name appears in both.
    payee_words = _words(debit_payee)
    for keyword in STRONG_EVIDENCE_KEYWORDS:
        if _has_phrase(memo_words, keyword) and _has_phrase(payee_words, keyword):
            return 0.15

    # Strong: the memo contains the payee's first distinctive word, when it is 4 letters or more.
    payee_first = next((w for w in payee_words.split() if w not in GENERIC_WORDS), "")
    if len(payee_first) >= 4 and payee_first.isalpha() and _has_phrase(memo_words, payee_first):
        return 0.15

    # Weak: a generic spending word in the memo.
    for keyword in WEAK_EVIDENCE_KEYWORDS:
        if _has_phrase(memo_words, keyword):
            return 0.07
    return 0.0


def _words(text: str | None) -> str:
    """Lowercase words of the text, single-spaced, with punctuation as the separator."""
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _has_phrase(words: str, phrase: str) -> bool:
    return f" {_words(phrase)} " in f" {words} "


def amount_boost(amount_dollars: float) -> float:
    """Up to 0.15 for large amounts, which are rarer and so stronger evidence."""
    if amount_dollars < AMOUNT_BOOST_THRESHOLD:
        return 0.0
    log_boost = (math.log10(amount_dollars) - 2.0) * AMOUNT_BOOST_MULTIPLIER
    return min(0.15, max(0.0, log_boost))


def compute_p2p_confidence(debit: TxnView, inflow: TxnView, memo: str) -> float:
    """Score a debit against an inflow: 0.60, plus or minus the boosts, capped at 0.90."""
    score = BASE_P2P_CONFIDENCE
    score += rounding_penalty(debit, inflow)
    score += evidence_boost(memo, debit.description)
    score += amount_boost(abs(debit.amount_cents) / 100)
    return min(P2P_CONFIDENCE_CAP, max(0.0, score))


def rule_p2p(pool: Pool, emit: Emitter) -> None:
    """Match each P2P inflow to the one debit that fits, or to the best of several.

    One candidate: review at 0.90 or more, otherwise manual. Several: the highest-scoring debit,
    as manual. None: nothing is proposed, because a payment from a person has no second side to
    link. The inflow still leaves the pool, so a merchant-return rule never reads it as a refund;
    it is listed as Not matched instead. Matched pairs are reserved even for manual proposals so
    later rules do not offer competing explanations in the same run.
    """
    inflows = [t for t in pool if t.amount_cents > 0 and P2P_INFLOW_RE.search(t.description)]
    debits = [
        t
        for t in pool
        if t.amount_cents < 0
        and (t.category_name or "") not in P2P_EXCLUDED_CATEGORIES
        and not is_transfer_like(t, emit)
    ]

    for inflow in inflows:
        if inflow.id not in pool:
            continue
        candidates = [
            d
            for d in debits
            if d.id in pool
            and abs(abs(d.amount_cents) - inflow.amount_cents) <= 1
            and 0 <= days_after(inflow, d) <= WINDOW_DAYS
            and emit.allowed(d, inflow)
        ]
        memo = _inflow_text(inflow)

        if len(candidates) == 1:
            debit = candidates[0]
            conf = compute_p2p_confidence(debit, inflow, memo)
            claim(
                pool,
                emit,
                rule="p2p",
                kind=LinkKind.p2p,
                first=debit,
                second=inflow,
                queue=Queue.review if conf >= REVIEW_THRESHOLD else Queue.manual,
                confidence=conf,
                drain=True,
                notes=f"conf={conf:.2f}",
            )
        elif len(candidates) > 1:
            scored = sorted(
                ((d, compute_p2p_confidence(d, inflow, memo)) for d in candidates),
                key=lambda pair: -pair[1],
            )
            best, best_conf = scored[0]
            claim(
                pool,
                emit,
                rule="p2p",
                kind=LinkKind.p2p,
                first=best,
                second=inflow,
                queue=Queue.manual,
                confidence=best_conf,
                drain=True,
                notes=f"ambiguous: {len(candidates)} debits, top conf={best_conf:.2f}",
            )
        else:
            pool.take(inflow.id)


def _inflow_text(inflow: TxnView) -> str:
    """The text that explains an inflow: its source description, then your own memo."""
    return " ".join(part for part in (inflow.description, inflow.memo) if part)
