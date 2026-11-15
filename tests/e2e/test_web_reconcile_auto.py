"""Links made automatically after an import, and what you do to them.

Statements are written into a temporary data directory and imported through the CLI; decisions go
through the Reconcile routes. Nothing here touches ~/omsync-data.
"""

import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner
from web_support import MakeClient, sign_in

from omsync import cli
from omsync.ledger.links import Link, RejectedProposal
from omsync.reconcile.engine import run
from omsync.reconcile.model import signature

START, END = date(2026, 7, 1), date(2026, 9, 30)
RANGE = {"start": "2026-07-01", "end": "2026-09-30"}
CHECKING, SAVINGS = "000012345678", "000055556666"
OFX_HEAD = """OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE

<OFX>
<BANKMSGSRSV1>
<STMTTRNRS>
<STMTRS>
<CURDEF>USD
<BANKACCTFROM>
<BANKID>000000001
<ACCTID>{account}
<ACCTTYPE>{kind}
</BANKACCTFROM>
<BANKTRANLIST>
"""
OFX_ROW = """<STMTTRN>
<TRNTYPE>{kind}
<DTPOSTED>202608{day:02d}120000[-5:EST]
<TRNAMT>{amount}
<FITID>{fitid}
<NAME>{name}
<MEMO>{memo}
</STMTTRN>
"""
OFX_TAIL = """</BANKTRANLIST>
<LEDGERBAL>
<BALAMT>1000.00
<DTASOF>20260831120000[-5:EST]
</LEDGERBAL>
</STMTRS>
</STMTTRNRS>
</BANKMSGSRSV1>
</OFX>
"""
Row = tuple[int, int, str, str]  # day of August 2026, cents, NAME, MEMO


def cli_run(*args: str | Path):
    return CliRunner().invoke(cli.app, [str(arg) for arg in args])


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A migrated database with a Chase checking and savings account mapped."""
    monkeypatch.setenv("OMSYNC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OMSYNC_CONFIG", str(tmp_path / "absent.toml"))
    assert cli_run("db", "upgrade").exit_code == 0
    for account, name, kind in (
        (CHECKING, "Checking", "checking"),
        (SAVINGS, "Savings", "savings"),
    ):
        added = cli_run(
            "accounts", "add", "--institution", "chase", "--institution-name", "Chase",
            "--statement-account", account, "--name", name, "--type", kind,
        )  # fmt: skip
        assert added.exit_code == 0, added.output
    (tmp_path / "statements" / "CHASE").mkdir(parents=True)
    return tmp_path


def statement(data_dir: Path, name: str, account: str, kind: str, rows: list[Row]) -> None:
    body = OFX_HEAD.format(account=account, kind=kind.upper())
    for index, (day, cents, label, memo) in enumerate(rows):
        amount = f"{cents / 100:.2f}"
        row_kind = "CREDIT" if cents > 0 else "DEBIT"
        fitid = f"{name}{index}"
        body += OFX_ROW.format(
            kind=row_kind, day=day, amount=amount, fitid=fitid, name=label, memo=memo
        )
    (data_dir / "statements" / "CHASE" / name).write_text(body + OFX_TAIL)


def zelle_pair(data_dir: Path, name: str, day: int, cents: int, conf: str) -> None:
    """A Zelle transfer from checking to savings that both statements name with one Conf#."""
    statement(
        data_dir, f"{name}_out.ofx", CHECKING, "checking",
        [(day, -cents, "Zelle payment to Self;", f"Conf# {conf}")],
    )  # fmt: skip
    statement(
        data_dir, f"{name}_in.ofx", SAVINGS, "savings",
        [(day, cents, "Zelle payment from Self;", f"Conf# {conf}")],
    )  # fmt: skip


def import_all(data_dir: Path):
    result = cli_run("import", "--apply")
    assert result.exit_code == 0, result.output
    return result


def database(client: TestClient) -> Session:
    return Session(client.app.state.engine)


def links(client: TestClient) -> list[Link]:
    with database(client) as db:
        return list(db.scalars(select(Link).order_by(Link.id)))


def rejections(client: TestClient) -> list[RejectedProposal]:
    with database(client) as db:
        return list(db.scalars(select(RejectedProposal).order_by(RejectedProposal.id)))


def unlink(client: TestClient, link_id: int, key: str):
    return client.post(f"/reconcile/unlink/{link_id}", data={**RANGE, "idempotency_key": key})


def test_review_and_manual_proposals_are_never_applied_automatically(
    data_dir: Path, make_client: MakeClient
) -> None:
    zelle_pair(data_dir, "auto", 3, 10_000, "abc1234567")
    # A refund on the other account (review), and a refund that fits two purchases (manual).
    statement(
        data_dir, "checking.ofx", CHECKING, "checking",
        [(4, -2_500, "ACME MARKET", ""), (4, -900, "ACME CAFE", ""), (5, -900, "ACME CAFE", "")],
    )  # fmt: skip
    statement(
        data_dir, "savings.ofx", SAVINGS, "savings",
        [(10, 2_500, "ACME MARKET", ""), (12, 900, "ACME CAFE", "")],
    )  # fmt: skip

    result = import_all(data_dir)

    assert "Linked 1 link automatically" in result.output
    client = make_client()
    sign_in(client)
    assert [link.evidence["rule"] for link in links(client)] == ["zelle_same_confirmation"]
    with database(client) as db:
        queues = sorted(d.queue.value for d in run(db, start=START, end=END).drafts)
    assert set(queues) == {"manual", "review"}
    assert queues.count("review") == 1

    # Rejecting the review proposal keeps its rule and evidence, and shows in the tally.
    with database(client) as db:
        [review] = [d for d in run(db, start=START, end=END).drafts if d.queue.value == "review"]
    rejected = client.post(
        "/reconcile/reject",
        data={**RANGE, "signature": signature(review), "idempotency_key": "r1"},
    )
    assert rejected.status_code == 200
    [row] = rejections(client)
    assert (row.reason.value, row.rule, row.kind.value) == ("rejected", review.rule_key, "refund")
    assert row.evidence
    undo = re.search(r"/batches/(\d+)/undo", rejected.text)
    assert undo
    assert client.post(f"/batches/{undo.group(1)}/undo").status_code == 200
    assert rejections(client) == []


def test_a_linked_row_opens_both_transactions_of_its_link(
    data_dir: Path, make_client: MakeClient
) -> None:
    zelle_pair(data_dir, "pair", 3, 10_000, "abc7654321")
    statement(data_dir, "other.ofx", CHECKING, "checking", [(5, -700, "ACME CAFE", "")])
    import_all(data_dir)
    client = make_client()
    sign_in(client)

    register = client.get("/transactions").text
    pair = re.search(r'href="(/transactions\?f=[^"]+)"', register)

    assert pair is not None, register
    shown = client.get(pair.group(1).replace("&amp;", "&")).text
    assert "2 linked transactions" in shown
    assert "Zelle payment to Self" in shown
    assert "Zelle payment from Self" in shown
    assert "ACME CAFE" not in shown
