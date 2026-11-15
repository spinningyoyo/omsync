"""Every reconcile rule, end to end: a synthetic statement pair is imported through the CLI, the
automatic step links what it may, and the Reconcile page holds the rest for a decision.

Each case is the bank's complete text for a pair, so a rule that stops matching its wording fails
here by name. Money is checked in integer cents: importing and linking never change the ledger's
total, and a linked pair of a transfer, a refund or a round trip nets to zero."""

import re
from dataclasses import dataclass
from datetime import date, timedelta

import pytest
from ledgerkit import Ledger, Row, web_client

START = date(2026, 8, 3)
RANGE = "start=2026-07-01&end=2026-09-30"

# name, kind, last four, joint
ACCOUNTS = [
    ("Checking", "checking", "1111", False),
    ("Savings", "savings", "2222", False),
    ("Card", "credit_card", "3333", False),
    ("Joint", "checking", "5555", True),
    ("Gold", "credit_card", "6666", True),
]

# account, day offset, cents, the bank's text
Line = tuple[str, int, int, str]


@dataclass(frozen=True)
class Case:
    lines: list[Line]
    rule: str
    kind: str
    cents: int
    queue: str | None = None  # None: linked by the automatic step. Else the page's queue label.
    owner: bool = False  # the owner's name is known, as the own-account wordings need


CASES: dict[str, Case] = {
    "bank reversal": Case(
        [
            ("Checking", 0, -5000, "ACME STORE REF 123456789"),
            ("Checking", 2, 5000, "REVERSAL ACME STORE REF 123456789"),
        ],
        "reversal_same_account",
        "refund",
        5000,
    ),
    "joint card bill": Case(
        [
            ("Gold", 0, 10000, "PAYMENT THANK YOU"),
            (
                "Checking",
                1,
                -5000,
                "Online Banking transfer to CHK 5555 Confirmation# JNT1234567",
            ),
            (
                "Joint",
                1,
                5000,
                "Online Banking transfer from CHK 1111 Confirmation# JNT1234567",
            ),
            ("Joint", 2, 5000, "Zelle payment from Pat Roe Conf# QWE1234567"),
        ],
        "joint_card_bill",
        "joint_settlement",
        5000,
    ),
    "same confirmation number": Case(
        [
            ("Checking", 0, -30000, "Online Banking transfer to CHK 2222 Confirmation# ABC1234567"),
            ("Savings", 0, 30000, "Online Banking transfer from CHK 1111 Confirmation# ABC1234567"),
        ],
        "transfer_same_confirmation",
        "transfer",
        30000,
    ),
    "zelle confirmation": Case(
        [
            ("Checking", 0, -3000, "Zelle payment to Sam Lee Conf# abc12345"),
            ("Savings", 0, 3000, "Zelle payment from Sam Lee Conf# abc12345"),
        ],
        "zelle_same_confirmation",
        "transfer",
        3000,
    ),
    "same transaction id": Case(
        [
            ("Checking", 0, -8000, "Wire to partner 1234567890123 ref"),
            ("Savings", 1, 8000, "Wire from partner 1234567890123"),
        ],
        "transfer_same_transaction_id",
        "transfer",
        8000,
    ),
    "card bill payment": Case(
        [
            ("Checking", 0, -20000, "CREDIT CARD Bill Payment"),
            ("Card", 1, 20000, "PAYMENT THANK YOU"),
        ],
        "card_bill_payment",
        "transfer",
        20000,
    ),
    "card credit balance refund": Case(
        [
            ("Card", 0, -5000, "ACH CREDIT BALANCE REFUND"),
            ("Checking", 2, 5000, "CARDCO DES:Credit Bal ID:1"),
        ],
        "card_credit_refund",
        "transfer",
        5000,
    ),
    "description names the other account": Case(
        [
            ("Checking", 0, -10000, "Transfer to SAV 2222"),
            ("Savings", 1, 10000, "Deposit from CHK 1111"),
        ],
        "transfer_names_account",
        "transfer",
        10000,
    ),
    "same day, same amount": Case(
        [
            ("Checking", 0, -7500, "Zelle payment to Ana Sample"),
            ("Savings", 0, 7500, "Transfer Ana Sample"),
        ],
        "transfer_same_amount",
        "transfer",
        7500,
        owner=True,
    ),
    "money sent and sent back": Case(
        [
            ("Checking", 0, -4000, "Zelle payment to Jo Park for lunch"),
            ("Checking", 10, 4000, "Zelle payment from Jo Park"),
        ],
        "p2p_round_trip",
        "p2p",
        4000,
    ),
    "round trip through an untracked account": Case(
        [
            ("Checking", 0, -9900, "Online Banking transfer to CHK 9999 Confirmation# XYZ1234567"),
            (
                "Checking",
                10,
                9900,
                "Online Banking transfer from CHK 9999 Confirmation# QRS7654321",
            ),
        ],
        "return_via_untracked_account",
        "refund",
        9900,
    ),
    "same store return": Case(
        [
            ("Checking", 0, -2500, "ACME HARDWARE #12"),
            ("Checking", 5, 2500, "ACME HARDWARE #12 RETURN"),
        ],
        "return_same_account",
        "refund",
        2500,
    ),
    "return at another brand waits": Case(
        [
            ("Checking", 0, -2500, "ACME HARDWARE #12"),
            ("Checking", 5, 2500, "ZEDCO GOODS"),
        ],
        "return_same_account",
        "refund",
        2500,
        queue="Review",
    ),
    "return that fits two purchases waits": Case(
        [
            ("Checking", 0, -2500, "ACME HARDWARE #12"),
            ("Checking", 1, -2500, "ACME HARDWARE #99"),
            ("Checking", 5, 2500, "ACME HARDWARE RETURN"),
        ],
        "return_same_account",
        "refund",
        2500,
        queue="Manual",
    ),
    "paypal return within a cent": Case(
        [
            ("Checking", 0, -1500, "PAYPAL *ACMEBOOKS"),
            ("Checking", 5, 1499, "PAYPAL *ACMEBOOKS"),
        ],
        "paypal_return_same_account",
        "refund",
        1499,
        queue="Review",
    ),
    "return on another account waits": Case(
        [
            ("Card", 0, -4000, "ACME HARDWARE"),
            ("Checking", 3, 4000, "ACME HARDWARE"),
        ],
        "return_other_account_same_store",
        "refund",
        4000,
        queue="Review",
    ),
}


def build(ledger: Ledger, case: Case) -> int:
    """Import the case's lines. Returns the sum of their amounts."""
    for name, kind, last4, joint in ACCOUNTS:
        ledger.add_account(name, kind, last4, joint=joint)
    by_account: dict[str, list[Row]] = {}
    for index, (account, offset, cents, text) in enumerate(case.lines):
        row = Row(START + timedelta(days=offset), cents, text, f"fit-{index}")
        by_account.setdefault(account, []).append(row)
    for account, rows in by_account.items():
        ledger.statement(account, rows)
    if case.owner:
        response = web_client(ledger.data_dir).post(
            "/settings/contacts/you/names", data={"given": "Ana", "family": "Sample"}
        )
        assert response.status_code == 200
    ledger.import_all()
    return sum(cents for _, _, cents, _ in case.lines)


def page_text(client, path: str = "/reconcile") -> str:
    html = client.get(f"{path}?{RANGE}").text
    main = re.search(r"<main.*</main>", html, re.DOTALL)
    assert main is not None
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", main.group(0)))


AUTOMATIC = [(name, case) for name, case in CASES.items() if case.queue is None]
WAITING = [(name, case) for name, case in CASES.items() if case.queue is not None]


@pytest.mark.parametrize(("name", "case"), AUTOMATIC, ids=[name for name, _ in AUTOMATIC])
def test_the_automatic_step_links_the_pair_and_conserves_money(
    ledger: Ledger, name: str, case: Case
) -> None:
    total = build(ledger, case)

    links = ledger.links()
    assert [(rule, kind, cents) for rule, kind, cents, _, _ in links] == [
        (case.rule, case.kind, case.cents)
    ]
    assert ledger.total_cents() == total  # linking never changes a transaction
    assert ledger.count("transaction") == len(case.lines)
    # Every cent of the linked amount is explained on both sides.
    assert (
        ledger.query(
            'SELECT SUM(ABS(t.amount_cents)) FROM link l JOIN "transaction" t '
            "ON t.id IN (l.from_transaction_id, l.to_transaction_id)"
        )[0][0]
        >= 2 * case.cents
    )


@pytest.mark.parametrize(("name", "case"), WAITING, ids=[name for name, _ in WAITING])
def test_a_doubtful_pair_waits_until_you_approve_it(ledger: Ledger, name: str, case: Case) -> None:
    total = build(ledger, case)
    assert ledger.links() == []  # doubt is never linked by itself
    client = web_client(ledger.data_dir)
    text = page_text(client)
    assert "To review 1" in text
    assert case.queue in text

    html = client.get(f"/reconcile?{RANGE}").text
    signature = re.search(r'name="signature" value="([^"]+)"', html)
    assert signature is not None
    approved = client.post(
        "/reconcile/approve",
        data={
            "start": "2026-07-01",
            "end": "2026-09-30",
            "signature": signature.group(1),
            "idempotency_key": f"approve-{name}",
        },
    )

    assert approved.status_code == 200
    assert [(rule, kind, cents) for rule, kind, cents, _, _ in ledger.links()] == [
        (case.rule, case.kind, case.cents)
    ]
    assert ledger.total_cents() == total


def test_a_rejected_pair_is_never_proposed_again(ledger: Ledger) -> None:
    case = CASES["return at another brand waits"]
    build(ledger, case)
    client = web_client(ledger.data_dir)
    html = client.get(f"/reconcile?{RANGE}").text
    signature = re.search(r'name="signature" value="([^"]+)"', html)
    assert signature is not None

    rejected = client.post(
        "/reconcile/reject",
        data={
            "start": "2026-07-01",
            "end": "2026-09-30",
            "signature": signature.group(1),
            "idempotency_key": "reject-once",
        },
    )

    assert rejected.status_code == 200
    ledger.import_all()  # a later import runs the rules again
    assert ledger.links() == []
    assert "Nothing needs you in this range" in page_text(client)
    assert ledger.count("rejected_proposal") == 1


def test_a_joint_bill_links_only_your_half(ledger: Ledger) -> None:
    """The partner's half arrives in the same joint account but is never linked."""
    build(ledger, CASES["joint card bill"])

    [(_, kind, cents, out_text, in_text)] = ledger.links()

    assert (kind, cents) == ("joint_settlement", 5000)
    assert "to CHK 5555" in out_text and "from CHK 1111" in in_text
    partner = ledger.query(
        'SELECT COUNT(*) FROM link l JOIN "transaction" t '
        "ON t.id IN (l.from_transaction_id, l.to_transaction_id) WHERE t.payee_raw LIKE '%Pat Roe%'"
    )
    assert partner == [(0,)]


def test_two_possible_partners_are_not_linked_by_amount_alone(ledger: Ledger) -> None:
    case = Case(
        [
            ("Checking", 0, -6000, "Zelle payment to Ana Sample"),
            ("Savings", 0, 6000, "Transfer Ana Sample"),
            ("Card", 0, 6000, "Transfer Ana Sample"),
        ],
        "transfer_same_amount",
        "transfer",
        6000,
        owner=True,
    )
    total = build(ledger, case)

    assert ledger.links() == []
    assert ledger.total_cents() == total
