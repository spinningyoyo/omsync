"""`omsync accounts`: list accounts, add one, or map another statement account ID to one.

Statement account IDs are shown only as ••last4, through last4_of, and never printed whole.
"""

from typing import Annotated
from uuid import uuid4

import typer
from sqlalchemy import select

from omsync.cli.common import fail, ready_session
from omsync.cli.groups import accounts_app
from omsync.ledger.accounts import AccountError, add_alias, create_account, find_account, last4_of
from omsync.ledger.models import Account, AccountType, Institution
from omsync.ledger.sources import account_digits


@accounts_app.command("list")
def accounts_list() -> None:
    """List accounts: id, institution, name, type, whether they are joint, and ••last4 with any
    newer digits the bank knows the account by."""
    with ready_session() as (_settings, session):
        rows = session.execute(
            select(Account, Institution.key)
            .join(Institution, Institution.id == Account.institution_id)
            .order_by(Account.id)
        ).all()
        if not rows:
            typer.echo("No accounts yet. Add one with `omsync accounts add`.")
            return
        typer.echo(f"{'id':>4}  {'institution':<12}  {'name':<20}  {'type':<12}  joint  last4")
        for account, institution_key in rows:
            digits = account_digits(account.last4, account.alias_last4) or "••????"
            joint = "yes" if account.joint else "no"
            typer.echo(
                f"{account.id:>4}  {institution_key:<12}  {account.name:<20}  "
                f"{account.type.value:<12}  {joint:<5}  {digits}"
            )


@accounts_app.command("add")
def accounts_add(
    institution: Annotated[str, typer.Option("--institution", help="Institution key, e.g. boa.")],
    institution_name: Annotated[
        str, typer.Option("--institution-name", help="Name for the institution, if it is new.")
    ],
    statement_account: Annotated[
        str,
        typer.Option(
            "--statement-account",
            help="The account ID the statement files carry, or the BoA folder name.",
        ),
    ],
    name: Annotated[str, typer.Option("--name", help="Account name, e.g. Checking.")],
    account_type: Annotated[AccountType, typer.Option("--type", help="The type of account.")],
    last4: Annotated[
        str | None,
        typer.Option(
            "--last4", help="Four characters to show as ••XXXX. Default: last four of ID."
        ),
    ] = None,
    joint: Annotated[bool, typer.Option("--joint", help="Mark the account as joint.")] = False,
) -> None:
    """Add an account at an institution, mapped to one statement account ID, as a batch."""
    key = institution.strip().lower()
    label = last4 if last4 is not None else last4_of(statement_account)
    if len(label) != 4:
        fail("--last4 must be exactly four characters")
    with ready_session() as (_settings, session):
        try:
            create_account(
                session,
                institution_key=key,
                institution_name=institution_name,
                statement_account_id=statement_account,
                name=name,
                account_type=account_type,
                last4=label,
                joint=joint,
                idempotency_key=f"accounts-add:{uuid4()}",
            )
        except AccountError as exc:
            fail(str(exc))
        created = find_account(session, key, statement_account)
        assert created is not None  # create_account just mapped this ID
        typer.echo(f"Added account {created.id} ({name}) at {key}, shown as ••{label}.")


@accounts_app.command("alias")
def accounts_alias(
    account_id: Annotated[int, typer.Option("--account-id", help="The account to map to.")],
    institution: Annotated[str, typer.Option("--institution", help="Institution key, e.g. boa.")],
    statement_account: Annotated[
        str,
        typer.Option(
            "--statement-account",
            help="The new statement account ID, or only a reissued card's new last four digits.",
        ),
    ],
) -> None:
    """Map another statement account ID to an existing account, e.g. after a card is reissued.

    With only the new card's last four, the bank connector looks for the account by them, and the
    first file it fetches for them is linked to this account too."""
    key = institution.strip().lower()
    with ready_session() as (_settings, session):
        try:
            add_alias(
                session,
                account_id=account_id,
                institution_key=key,
                statement_account_id=statement_account,
                idempotency_key=f"accounts-alias:{uuid4()}",
            )
        except AccountError as exc:
            fail(str(exc))
    typer.echo(f"Mapped statement account ••{last4_of(statement_account)} to account {account_id}.")
