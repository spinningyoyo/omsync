"""The command groups. Each command module registers its commands on one of these apps."""

import typer

app = typer.Typer(help="omsync: a local, single-user personal finance tracker.")
db_app = typer.Typer(
    help="Database commands. `upgrade` backs up, then migrates to the head. `backup` copies it. "
    "`snapshot` makes a migrated copy to inspect."
)
accounts_app = typer.Typer(help="Accounts: list them, add one, or map a statement account to one.")
batches_app = typer.Typer(help="Batches: list recent changes, or undo one.")
keywords_app = typer.Typer(
    help="Keywords: import a keyword file, test a payee against the lists, or list conflicts."
)
rules_app = typer.Typer(
    help="Rules: each sets a category when its conditions hold. The first that holds, in order, "
    "wins. `rules list` shows them."
)
providers_app = typer.Typer(help="Bank providers: list them. Edit them on the Data sync page.")
receipts_app = typer.Typer(
    help="Receipts: match a vendor's receipts to your transactions, list the vendors, or download."
)
fetch_app = typer.Typer(help="Download a vendor's receipts from its website into receipts/.")
backup_app = typer.Typer(
    help="Backups: archive the data directory, list the archives, or restore one. "
    "`db backup` copies only the database."
)
export_app = typer.Typer(help="Excel exports: a year's transactions, or the rental workbook.")
rental_app = typer.Typer(
    help="Rental properties: mark accounts as rentals, and set the categories that count as rental."
)
config_app = typer.Typer(help="Settings: show them, or write one to omsync.toml.")

app.add_typer(db_app, name="db")
app.add_typer(accounts_app, name="accounts")
app.add_typer(batches_app, name="batches")
app.add_typer(keywords_app, name="keywords")
app.add_typer(rules_app, name="rules")
app.add_typer(providers_app, name="providers")
app.add_typer(receipts_app, name="receipts")
receipts_app.add_typer(fetch_app, name="fetch")
app.add_typer(backup_app, name="backup")
app.add_typer(export_app, name="export")
app.add_typer(rental_app, name="rental")
app.add_typer(config_app, name="config")
