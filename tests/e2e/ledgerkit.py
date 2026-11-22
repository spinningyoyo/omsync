"""A synthetic ledger driven only through the CLI. Statements are written as OFX files into the
drop folder, imported with `omsync import --apply`, and read back with SQLite. Every name, number
and amount is made up. Nothing here reads the keychain or touches a real data directory."""

import re
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from typer.testing import CliRunner
from web_support import FakeOwnerCheck

from omsync import cli
from omsync.core.db import database_url, make_engine
from omsync.core.settings import Settings
from omsync.ledger.models import Provider, ProviderKind
from omsync.web.app import create_app

CARD = "credit_card"


def add_receipt_provider(data_dir: Path, connector: str, key: str = "default") -> None:
    """Add a receipt provider to a migrated data directory, without a batch, so the batch numbers a
    test counts stay its own. Its receipt files belong in receipts/<CONNECTOR>/<key>/<year>/."""
    engine = make_engine(database_url(Settings(data_dir=data_dir)))
    with Session(engine) as session:
        session.add(
            Provider(
                key=key,
                kind=ProviderKind.receipt,
                name=f"{connector.title()} {key}",
                connector_key=connector,
            )
        )
        session.commit()
    engine.dispose()


@dataclass(frozen=True)
class Row:
    """One statement row. `day` is the posted date. `name` is the bank's complete text."""

    day: date
    cents: int
    name: str
    fitid: str


def ofx(account_id: str, kind: str, rows: Iterable[Row], balance_cents: int | None = None) -> str:
    """An OFX 2 statement for one account. Card statements use the credit card message set."""
    card = kind == CARD
    rows = list(rows)
    body = "".join(
        f"<STMTTRN><TRNTYPE>{'CREDIT' if r.cents > 0 else 'DEBIT'}</TRNTYPE>"
        f"<DTPOSTED>{r.day:%Y%m%d}120000.000[0:GMT]</DTPOSTED>"
        f"<TRNAMT>{r.cents / 100:.2f}</TRNAMT><FITID>{r.fitid}</FITID>"
        f"<NAME>{r.name.replace('&', '&amp;')}</NAME></STMTTRN>"
        for r in rows
    )
    first = min(r.day for r in rows)
    last = max(r.day for r in rows)
    ledger = (
        f"<LEDGERBAL><BALAMT>{balance_cents / 100:.2f}</BALAMT>"
        f"<DTASOF>{last:%Y%m%d}120000.000[0:GMT]</DTASOF></LEDGERBAL>"
        if balance_cents is not None
        else ""
    )
    tran_list = (
        f"<BANKTRANLIST><DTSTART>{first:%Y%m%d}000000.000[0:GMT]</DTSTART>"
        f"<DTEND>{last:%Y%m%d}235959.000[0:GMT]</DTEND>{body}</BANKTRANLIST>"
    )
    if card:
        stmt = (
            "<CREDITCARDMSGSRSV1><CCSTMTTRNRS><TRNUID>1</TRNUID>"
            "<STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS><CCSTMTRS><CURDEF>USD</CURDEF>"
            f"<CCACCTFROM><ACCTID>{account_id}</ACCTID></CCACCTFROM>{tran_list}{ledger}"
            "</CCSTMTRS></CCSTMTTRNRS></CREDITCARDMSGSRSV1>"
        )
    else:
        stmt = (
            "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1</TRNUID>"
            "<STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS><STMTRS><CURDEF>USD</CURDEF>"
            f"<BANKACCTFROM><BANKID>000000001</BANKID><ACCTID>{account_id}</ACCTID>"
            f"<ACCTTYPE>{'SAVINGS' if kind == 'savings' else 'CHECKING'}</ACCTTYPE></BANKACCTFROM>"
            f"{tran_list}{ledger}</STMTRS></STMTTRNRS></BANKMSGSRSV1>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>'
        "<OFX><SIGNONMSGSRSV2><SONRS><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>"
        "<DTSERVER>20261231120000.000[0:GMT]</DTSERVER><LANGUAGE>ENG</LANGUAGE></SONRS>"
        f"</SIGNONMSGSRSV2>{stmt}</OFX>\n"
    )


class Ledger:
    """A data directory under tmp_path, driven through the CLI."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.accounts: dict[str, tuple[str, str]] = {}  # name -> (statement id, kind)
        self._counter = 0

    def cli(self, *args: object, ok: bool = True):
        result = CliRunner().invoke(cli.app, [str(a) for a in args])
        if ok:
            assert result.exit_code == 0, result.output
        return result

    def add_account(
        self,
        name: str,
        kind: str = "checking",
        last4: str = "0000",
        *,
        joint: bool = False,
        institution: str = "bank",
    ) -> None:
        """Create an account through `accounts add`. Its statement ID ends in `last4`."""
        if not self.accounts:
            self.cli("db", "upgrade")
        statement_id = f"00000000{last4}"
        args = [
            "accounts", "add", "--institution", institution, "--institution-name",
            institution.title(), "--statement-account", statement_id, "--name", name,
            "--type", kind,
        ]  # fmt: skip
        if joint:
            args.append("--joint")
        self.cli(*args)
        self.accounts[name] = (statement_id, kind)
        return statement_id

    def last4(self, name: str) -> str:
        return self.accounts[name][0][-4:]

    def statement(self, name: str, rows: Iterable[Row], *, file: str | None = None) -> Path:
        statement_id, kind = self.accounts[name]
        self._counter += 1
        folder = self.data_dir / "statements" / "BANK"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (file or f"{name.lower().replace(' ', '_')}_{self._counter}.ofx")
        path.write_text(ofx(statement_id, kind, rows), encoding="utf-8")
        return path

    def add_receipt_provider(self, connector: str, key: str = "default") -> None:
        add_receipt_provider(self.data_dir, connector, key)

    def import_all(self):
        return self.cli("import", "--apply")

    # Reads

    def query(self, sql: str, params: tuple = ()) -> list[tuple[Any, ...]]:
        with closing(sqlite3.connect(self.data_dir / "omsync.db")) as connection:
            return connection.execute(sql, params).fetchall()

    def links(self) -> list[tuple[str, str, int, str, str]]:
        """Every link as (rule, kind, cents, from payee, to payee), in a stable order."""
        return self.query(
            "SELECT json_extract(l.evidence, '$.rule'), l.kind, l.amount_cents, "
            "COALESCE(f.payee_raw, ''), COALESCE(t.payee_raw, '') FROM link l "
            'LEFT JOIN "transaction" f ON f.id = l.from_transaction_id '
            'LEFT JOIN "transaction" t ON t.id = l.to_transaction_id '
            "ORDER BY l.id"
        )

    def total_cents(self) -> int:
        return self.query('SELECT COALESCE(SUM(amount_cents), 0) FROM "transaction"')[0][0]

    def count(self, table: str) -> int:
        return self.query(f'SELECT COUNT(*) FROM "{table}"')[0][0]


def batch_id(output: str) -> int:
    match = re.search(r"batch (\d+)", output)
    assert match, output
    return int(match.group(1))


def web_client(data_dir: Path) -> TestClient:
    """The web app on a data directory the CLI has already migrated, signed in through the real
    sign-in route with a fake owner check and no vault."""
    client = TestClient(create_app(Settings(data_dir=data_dir), owner_check=FakeOwnerCheck()))
    response = client.post("/sign-in", data={"next": "/"}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return client


def plain(html: str) -> str:
    """The visible words of a page or fragment, whitespace collapsed."""
    main = re.search(r"<main.*</main>", html, re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", main.group(0) if main else html)
    return re.sub(r"\s+", " ", text)


def hidden_fields(html: str, action: str) -> dict[str, str]:
    """The hidden inputs of the form that posts to `action`, as the browser would send them."""
    form = re.search(rf'<form[^>]*hx-post="{re.escape(action)}".*?</form>', html, re.DOTALL)
    assert form, f"no form posts to {action}"
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form.group(0)))
