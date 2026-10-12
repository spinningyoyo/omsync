"""What the Reconcile page proposes for synthetic ledgers, and how its decisions hold on a rerun.

Each case seeds a temporary database, reads the page, runs the engine, approves through the
route, and runs again: the rest must stay as proposed, and nothing rejected or approved returns.
Descriptions are the statement's complete text. All names, digits, and IDs are made up."""

from collections.abc import Callable
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from support import sign_in

from omsync.ledger.categories import Category
from omsync.ledger.models import Account, AccountType, Institution, Transaction
from omsync.reconcile.engine import run
from omsync.reconcile.model import Draft, signature

START, END = date(2026, 7, 1), date(2026, 9, 30)
RANGE = "start=2026-07-01&end=2026-09-30"
DAY0 = date(2026, 8, 3)

ACCOUNTS: dict[str, tuple[str, AccountType, str, bool]] = {
    "checking": ("Checking", AccountType.checking, "1111", False),
    "savings": ("Savings", AccountType.savings, "2222", False),
    "card": ("Card", AccountType.credit_card, "3333", False),
    "card2": ("Card Two", AccountType.credit_card, "4444", False),
    "joint": ("Joint Checking", AccountType.checking, "5555", True),
    "gold": ("Gold Card", AccountType.credit_card, "6666", True),
    "blue": ("Blue Card", AccountType.credit_card, "7777", True),
}

# key, account, day offset, amount, description, category
Txn = tuple[str, str, int, str, str, str | None]
# rule, queue, money-out key, money-in key (None for a joint settlement without its inflow)
Expected = tuple[str, str, str | None, str | None]


def t(key: str, account: str, day: int, amount: str, text: str, category: str | None = None) -> Txn:
    return key, account, day, amount, text, category


@pytest.fixture
def seed(app: FastAPI) -> Callable[..., dict[str, int]]:
    """Return a function that creates the accounts and the given transactions, keyed by name."""

    def build(txns: list[Txn], *, memos: dict[str, str] | None = None) -> dict[str, int]:
        with Session(app.state.engine) as db:
            bank = Institution(key="bank-a", name="Bank A")
            db.add(bank)
            db.flush()
            accounts = {
                key: Account(institution_id=bank.id, name=name, type=kind, last4=last4, joint=joint)
                for key, (name, kind, last4, joint) in ACCOUNTS.items()
            }
            db.add_all(accounts.values())
            categories = {name: Category(name=name) for name in ("Family_Joint", "Transfer")}
            db.add_all(categories.values())
            db.flush()
            ids: dict[str, int] = {}
            for key, account, day, amount, text, category in txns:
                row = Transaction(
                    account_id=accounts[account].id,
                    posted_on=DAY0 + timedelta(days=day),
                    amount_cents=round(float(amount) * 100),
                    payee=text,
                    payee_raw=text,
                    memo=(memos or {}).get(key),
                    category_id=categories[category].id if category else None,
                )
                db.add(row)
                db.flush()
                ids[key] = row.id
            db.commit()
        return ids

    return build


def drafts_of(app: FastAPI) -> tuple[Draft, ...]:
    with Session(app.state.engine) as db:
        return run(db, start=START, end=END).drafts


def summarize(drafts: tuple[Draft, ...], ids: dict[str, int]) -> set[Expected]:
    key_of = {txn_id: key for key, txn_id in ids.items()}
    return {
        (
            d.rule_key,
            d.queue.value,
            key_of.get(d.out_txn_id) if d.out_txn_id else None,
            key_of.get(d.in_txn_id) if d.in_txn_id else None,
        )
        for d in drafts
    }


# Shared transaction IDs


def id_pair(n: int, amount: str = "200.00") -> list[Txn]:
    return [
        t(
            f"out{n}",
            "checking",
            0,
            f"-{amount}",
            f"JPMorgan Chase DES:Ext Trnsfr ID:2719701135{n} INDN:SAMPLE USER CO ID:9000000001 WEB",
        ),
        t(f"in{n}", "savings", 0, amount, f"Online Transfer 2719701135{n} from BOA SAMPLE 1111"),
    ]


def test_shared_ids_keep_resolving_after_part_of_them_is_approved(
    client: TestClient, app: FastAPI, seed
) -> None:
    sign_in(client)
    ids = seed([txn for n in range(1, 5) for txn in id_pair(n)])
    drafts = drafts_of(app)
    [first, second] = [d for d in drafts if d.out_txn_id in (ids["out1"], ids["out2"])]

    response = client.post(
        "/reconcile/approve",
        data={
            "start": "2026-07-01",
            "end": "2026-09-30",
            "signature": [signature(first), signature(second)],
            "idempotency_key": "partial",
        },
    )

    assert response.status_code == 200
    assert summarize(drafts_of(app), ids) == {
        ("1c", "auto_approve", f"out{n}", f"in{n}") for n in (3, 4)
    }


def test_a_rejected_id_pair_does_not_come_back(client: TestClient, app: FastAPI, seed) -> None:
    sign_in(client)
    ids = seed(id_pair(1))
    [draft] = drafts_of(app)

    response = client.post(
        "/reconcile/reject",
        data={
            "start": "2026-07-01",
            "end": "2026-09-30",
            "signature": signature(draft),
            "idempotency_key": "rej",
        },
    )

    assert response.status_code == 200
    assert summarize(drafts_of(app), ids) == set()


# Card payments

# Account references

# Joint settlements

# P2P scoring


# Not matched


# Forwarded reimbursements
