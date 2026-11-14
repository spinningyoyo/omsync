"""`omsync sync` and `omsync sync-log`: fetch statements through providers, and read the sync log.

Sync unlocks the vault first, asking for the master password with a hidden prompt, then runs each
provider's connector. The password is passed to the vault and never printed or stored.
"""

from typing import Annotated

import typer
from sqlalchemy import select

from omsync.cli.common import fail, load_settings_or_exit, parse_period, ready_session
from omsync.cli.groups import app
from omsync.cli.imports import echo_import_report
from omsync.core.settings import Settings
from omsync.core.vault import BitwardenCLI, SystemKeychain, Vault, VaultError
from omsync.ingest.connectors.base import ConnectorError, DateRange
from omsync.ingest.models import SyncRun, SyncStatus
from omsync.ingest.sync import SyncBusy, SyncResult, sync_all, sync_provider


class TyperPrompt:
    """Asks you in the terminal. A secret answer is not echoed as it is typed. A QR stays in the
    bank's Chrome window, and Ctrl-C stops a sync, so there is no cancel."""

    def ask(self, question: str, *, secret: bool = False, digits: int | None = None) -> str:
        return typer.prompt(question, hide_input=secret)

    def show(self, message: str, *, image: str | None = None) -> None:
        if message:
            typer.echo(f"{message} The QR is in the Chrome window." if image else message)

    def cancelled(self) -> bool:
        return False


def make_vault(settings: Settings) -> Vault:
    """The vault the CLI unlocks. It shares Bitwarden state and keychain entries with the app."""
    return Vault(
        BitwardenCLI(appdata_dir=settings.data_dir / "bitwarden"),
        SystemKeychain(),
        settings.vault_idle_minutes,
    )


@app.command("sync")
def sync_command(
    provider: Annotated[
        str | None, typer.Argument(help="Provider key, such as chase_joint. Omit it with --all.")
    ] = None,
    all_providers: Annotated[
        bool, typer.Option("--all", help="Sync every enabled provider, one after another.")
    ] = False,
    from_date: Annotated[
        str | None, typer.Option("--from", help="First day to fetch, YYYY-MM-DD. Give with --to.")
    ] = None,
    to_date: Annotated[
        str | None, typer.Option("--to", help="Last day to fetch, YYYY-MM-DD. Give with --from.")
    ] = None,
    email: Annotated[
        str | None,
        typer.Option(
            "--email", help="Your Bitwarden email. Default: email in omsync.toml or OMSYNC_EMAIL."
        ),
    ] = None,
) -> None:
    """Fetch statement files with a provider's connector, then import them, one batch per file.

    Give a provider key or --all, not both. Asks for the vault master password first. With --all,
    a provider that fails does not stop the others. Exits 1 if any sync failed or needs you, or
    another is running.
    """
    _check_selection(provider, all_providers)
    period = _period(from_date, to_date)
    settings = load_settings_or_exit()
    address = (email or settings.email or "").strip()
    if not address:
        fail("no vault email: pass --email, or set email in omsync.toml or OMSYNC_EMAIL")

    vault = make_vault(settings)
    # The database is checked before the password is asked for, so a missing one costs no prompt.
    with ready_session() as (_settings, session):
        try:
            _unlock(vault, address)
            if all_providers:
                outcomes = sync_all(
                    session, settings, vault=vault, prompt=TyperPrompt(), period=period
                )
            else:
                result = sync_provider(
                    session,
                    settings,
                    provider_key=provider.strip().lower(),
                    vault=vault,
                    prompt=TyperPrompt(),
                    period=period,
                )
        except SyncBusy as exc:
            fail(f"{exc}. Try again when it finishes.")
        except ConnectorError as exc:
            fail(str(exc))
        finally:
            vault.lock()

    if not all_providers:
        _report_one(result, provider.strip().lower())
    else:
        _report_all(outcomes)


@app.command("sync-log")
def sync_log(
    limit: Annotated[
        int, typer.Option("--limit", min=1, help="How many to show, newest first.")
    ] = 20,
) -> None:
    """List recent syncs: when each started, its provider, connector, status, and files."""
    with ready_session() as (_settings, session):
        runs = session.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(limit)).all()
        if not runs:
            typer.echo("No syncs yet.")
            return
        for run in runs:
            when = run.started_at.strftime("%Y-%m-%d %H:%M UTC")
            provider = run.provider_key or run.institution_key
            line = (
                f"{run.id:>5}  {when:<20}  {provider:<16}  {run.connector_key:<12}  "
                f"{run.status.value:<18}  {_files(run.files_fetched):<12}  {run.message or ''}"
            )
            typer.echo(line.rstrip())


def _check_selection(provider: str | None, all_providers: bool) -> None:
    if provider is None and not all_providers:
        fail("give a provider key, or --all to sync every provider")
    if provider is not None and all_providers:
        fail("give a provider key or --all, not both")


def _report_one(result: SyncResult, provider_key: str) -> None:
    _echo_result(result, provider_key)
    if result.status is not SyncStatus.succeeded:
        fail(_failure(result))


def _report_all(outcomes: list) -> None:
    if not outcomes:
        typer.echo("No enabled providers to sync.")
        return
    failed: list[str] = []
    for outcome in outcomes:
        if outcome.result is None:
            typer.echo(f"{outcome.provider_key}: did not run: {outcome.error}")
            failed.append(outcome.provider_key)
            continue
        _echo_result(outcome.result, outcome.provider_key)
        if outcome.result.status is not SyncStatus.succeeded:
            typer.echo(f"{outcome.provider_key}: {_failure(outcome.result)}")
            failed.append(outcome.provider_key)
    if failed:
        fail(f"{len(failed)} of {len(outcomes)} providers did not sync: {', '.join(failed)}")


def _unlock(vault: Vault, address: str) -> None:
    password = typer.prompt("Master password", hide_input=True)
    try:
        vault.unlock(address, password)
    except VaultError as exc:
        fail(str(exc))


def _echo_result(result: SyncResult, provider_key: str) -> None:
    """Say what was fetched and for which period, then each file's import. A failed fetch has
    nothing fetched to report, so only the failure, printed by the caller, is shown."""
    if result.report is None and result.status is not SyncStatus.succeeded:
        return
    window = result.period
    typer.echo(
        f"Fetched {_files(result.files_fetched)} for {provider_key}, "
        f"{window.start.isoformat()} to {window.end.isoformat()}."
    )
    if result.report is not None:
        echo_import_report(result.report)
    if result.categorized:
        typer.echo(f"Categorized {result.categorized} transactions from keywords.")


def _failure(result: SyncResult) -> str:
    detail = f": {result.message}" if result.message else ""
    return f"sync {result.status.value}{detail}"


def _files(count: int) -> str:
    return f"{count} file" if count == 1 else f"{count} files"


def _period(from_date: str | None, to_date: str | None) -> DateRange | None:
    period = parse_period(from_date, to_date, start_flag="--from", end_flag="--to")
    return None if period is None else DateRange(start=period[0], end=period[1])
