"""`omsync providers`: the bank providers, which connector and login each uses, and its accounts.

Providers are added, edited, and deleted on the Data sync page of the web app, where each change is
a batch that `omsync batches undo` can reverse.
"""

import typer

from omsync.cli.common import ready_session
from omsync.cli.groups import providers_app
from omsync.ingest.connectors.config import list_providers


@providers_app.command("list")
def providers_list() -> None:
    """List each provider: institution, connector, vault login, and accounts it fetches."""
    with ready_session() as (_settings, session):
        providers = list_providers(session)
    if not providers:
        typer.echo("No providers yet. Add one on the Data sync page of the web app.")
        return
    for provider in providers:
        login = "login set" if provider.vault_item_id else "no login"
        accounts = len(provider.account_ids)
        state = "" if provider.enabled else "  (turned off)"
        typer.echo(
            f"{provider.key:<16}  {provider.institution_key:<14}  {provider.connector_key:<14}  "
            f"{login:<10}  {_accounts(accounts)}{state}"
        )


def _accounts(count: int) -> str:
    return f"{count} account" if count == 1 else f"{count} accounts"
