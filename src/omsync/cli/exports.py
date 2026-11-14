"""`omsync export` and `omsync rental`: the yearly Excel workbooks and their rental settings.

Exports read the ledger at the head revision and write one .xlsx file. They never change data.
Without --output a file goes to the exports directory under the data directory.
"""

from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select

from omsync.cli.common import fail, ready_session
from omsync.cli.groups import export_app, rental_app
from omsync.ledger import rental as rental_settings
from omsync.ledger.models import Account, Category
from omsync.ledger.sources import account_digits
from omsync.reports import exports as workbooks

_YEAR = {"min": 2000, "max": 2100}
_NO_RENTAL = "no rental accounts are set. Add one with `omsync rental add ACCOUNT_ID LABEL` first."


@export_app.command("transactions")
def export_transactions(
    year: Annotated[int, typer.Option("--year", help="Calendar year to export.", **_YEAR)],
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write the workbook here instead of the exports directory."),
    ] = None,
) -> None:
    """Write a year's transactions, with split rows, and the totals by category to an .xlsx file."""
    with ready_session() as (settings, session):
        try:
            path = workbooks.export_transactions(
                session, year, _explicit(output), exports_dir=settings.exports_dir
            )
        except OSError as exc:
            fail(f"could not write the workbook: {exc}")
    typer.echo(f"Wrote {path}")


@export_app.command("rental")
def export_rental(
    year: Annotated[int, typer.Option("--year", help="Calendar year to export.", **_YEAR)],
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Write the workbook here instead of the exports directory."),
    ] = None,
) -> None:
    """Write a year's rental workbook: income and expenses per property, by category and month."""
    with ready_session() as (settings, session):
        if not rental_settings.get_rental_accounts(session):
            fail(_NO_RENTAL)
        try:
            path = workbooks.export_rental(
                session, year, _explicit(output), exports_dir=settings.exports_dir
            )
        except workbooks.NoRentalAccounts:
            fail(_NO_RENTAL)
        except OSError as exc:
            fail(f"could not write the workbook: {exc}")
    typer.echo(f"Wrote {path}")


@rental_app.command("accounts")
def rental_accounts() -> None:
    """List the rental accounts: id, property label, account, and ••last4."""
    with ready_session() as (_settings, session):
        accounts = rental_settings.get_rental_accounts(session)
        if not accounts:
            typer.echo("No rental accounts yet. Add one with `omsync rental add ACCOUNT_ID LABEL`.")
            return
        found = {row.id: row for row in session.scalars(select(Account))}
        typer.echo(f"{'id':>4}  {'label':<24}  {'account':<20}  last4")
        for account_id, label in sorted(accounts.items(), key=lambda item: item[1].casefold()):
            account = found.get(account_id)
            name = account.name if account else "(missing)"
            last4 = "-"
            if account is not None:
                last4 = account_digits(account.last4, account.alias_last4) or "••????"
            typer.echo(f"{account_id:>4}  {label:<24}  {name:<20}  {last4}")


@rental_app.command("add")
def rental_add(
    account_id: Annotated[
        int, typer.Argument(help="The account's ID, from `omsync accounts list`.")
    ],
    label: Annotated[
        str, typer.Argument(help="The property's name, as its export sheet shows it.")
    ],
) -> None:
    """Mark an account as a rental property with a label, or relabel it."""
    with ready_session() as (_settings, session):
        try:
            rental_settings.set_rental_account(session, account_id, label)
        except ValueError as exc:
            fail(str(exc))
    typer.echo(f"Rental account {account_id} is {label.strip()}.")


@rental_app.command("remove")
def rental_remove(
    account_id: Annotated[
        int, typer.Argument(help="The account's ID, from `omsync rental accounts`.")
    ],
) -> None:
    """Stop treating an account as a rental property. Its transactions are not changed."""
    with ready_session() as (_settings, session):
        removed = rental_settings.remove_rental_account(session, account_id)
    if not removed:
        fail(f"account {account_id} is not a rental account")
    typer.echo(f"Removed rental account {account_id}. Its transactions are unchanged.")


@rental_app.command("categories")
def rental_categories(
    names: Annotated[
        list[str] | None,
        typer.Argument(
            help="Category names that count as rental expenses. With none, show the current list."
        ),
    ] = None,
) -> None:
    """Show, or with names set, replace the category names that count as rental expenses.

    Names match your category names exactly, so quote a name that has spaces.
    """
    with ready_session() as (_settings, session):
        if not names:
            for name in rental_settings.get_rental_categories(session):
                typer.echo(name)
            return
        try:
            saved = rental_settings.set_rental_categories(session, names)
        except ValueError as exc:
            fail(str(exc))
        known = set(session.scalars(select(Category.name)))
    typer.echo(f"Rental categories: {', '.join(saved)}")
    for name in saved:
        if name not in known:
            typer.echo(f"warning: no category named {name!r} yet")


def _explicit(output: Path | None) -> Path | None:
    """The --output path made absolute, or None when the option was not given."""
    return None if output is None else output.expanduser().resolve()
