"""`omsync receipts`: match a vendor's receipts to your transactions, list the vendors, or download.

`run` previews by default and writes nothing. It covers all history unless --start and --end give
a period, which limits what is planned to charges whose transaction posted inside it. With --apply
it writes the planned charges as one batch, which `omsync batches undo` can reverse. `vendors`
reads only the settings. `fetch costco` and `fetch amazon` sync a receipt profile like `omsync sync`
does: through Chrome, under the same lock, with a sync log entry.
"""

from datetime import date
from typing import Annotated
from uuid import uuid4

import typer
from sqlalchemy.orm import Session

from omsync.cli.common import fail, load_settings_or_exit, parse_period, ready_session
from omsync.cli.groups import fetch_app, receipts_app
from omsync.core.batches import BatchError
from omsync.ingest.connectors.base import ConnectorError, DateRange
from omsync.ingest.runs import SyncBusy
from omsync.ledger.models import SyncStatus
from omsync.receipts.profiles import VendorProfile, list_vendor_profiles, vendor_folder_path
from omsync.receipts.service import (
    ServiceReport,
    apply_report,
    registered_vendors,
    run_vendor,
    vendor_folder,
)
from omsync.sync.run import sync_one

_PREVIEW_ONLY = (
    "Preview only: nothing was written. Run again with --apply to apply {count} charges."
)


@receipts_app.command("run")
def receipts_run(
    vendor: Annotated[str, typer.Argument(help="A receipt vendor key, such as amazon.")],
    apply: Annotated[
        bool, typer.Option("--apply", help="Write the matched charges as one batch.")
    ] = False,
    start: Annotated[
        str | None,
        typer.Option(
            "--start",
            help="Only charges whose transaction posted on or after this date, YYYY-MM-DD. "
            "Give with --end.",
        ),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option(
            "--end",
            help="Only charges whose transaction posted on or before this date, YYYY-MM-DD. "
            "Give with --start.",
        ),
    ] = None,
) -> None:
    """Match a vendor's receipts to transactions. Previews unless --apply is given."""
    period = parse_period(start, end, start_flag="--start", end_flag="--end")
    first, last = period if period is not None else (None, None)
    with ready_session() as (settings, session):
        try:
            report = run_vendor(
                session, settings, vendor, today=date.today(), start=first, end=last
            )
        except KeyError as exc:
            fail(str(exc.args[0]))
        _print_report(report)
        if report.to_apply == 0:
            typer.echo("Nothing to apply.")
            return
        if not apply:
            typer.echo(_PREVIEW_ONLY.format(count=report.to_apply))
            return
        try:
            batch = apply_report(session, report, idempotency_key=f"receipts-{vendor}:{uuid4()}")
        except BatchError as exc:
            fail(str(exc))
        if batch is None:
            typer.echo("Nothing to apply.")
            return
        typer.echo(
            f"Applied {report.to_apply} charges as batch {batch.id}. "
            f"Undo with: omsync batches undo {batch.id}"
        )


@receipts_app.command("vendors")
def receipts_vendors() -> None:
    """List the receipt vendors omsync reads, and the folder of each one's providers."""
    settings = load_settings_or_exit()
    for vendor in registered_vendors():
        folder = vendor_folder(settings, vendor.key)
        state = "found" if folder.is_dir() else "no folder yet"
        typer.echo(f"{vendor.key:<10}  {folder}  ({state})")


class EnterPrompt:
    """Waits for you to press Enter, such as after you sign in to Costco in Chrome."""

    def ask(self, question: str, *, secret: bool = False, digits: int | None = None) -> str:
        return typer.prompt(question, default="", show_default=False, prompt_suffix=" ")

    def confirm(self, question: str, *, button: str) -> bool:
        typer.prompt(f"{question} Press Enter for {button}", default="", show_default=False)
        return True

    def show(self, message: str, *, image: str | None = None) -> None:
        if message:
            typer.echo(message)

    def cancelled(self) -> bool:
        return False


@fetch_app.command("costco")
def fetch_costco(
    start: Annotated[
        str | None,
        typer.Option("--start", help="First purchase day, YYYY-MM-DD. Give with --end."),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option("--end", help="Last purchase day, YYYY-MM-DD. Give with --start."),
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="The Costco profile's key, if you have several.")
    ] = None,
) -> None:
    """Download a Costco profile's receipts and save them, as a sync of that profile.

    Without --start and --end, it fetches from shortly before the newest receipt it has. Chrome
    opens with omsync's browser profile. Sign in to Costco there, then press Enter here. Receipts
    already saved are skipped. Exits 1 if you need to act, or the download fails. Another sync
    refuses to start.
    """
    _fetch("costco", start, end, profile)


@fetch_app.command("amazon")
def fetch_amazon(
    start: Annotated[
        str | None,
        typer.Option("--start", help="First purchase day, YYYY-MM-DD. Give with --end."),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option("--end", help="Last purchase day, YYYY-MM-DD. Give with --start."),
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="The Amazon profile's key, if you have several.")
    ] = None,
) -> None:
    """Download an Amazon profile's orders and refunds and save them, as a sync of that profile.

    Without --start and --end, it fetches from shortly before the newest receipt it has. Chrome
    opens with omsync's browser profile. Sign in to Amazon there, then press Enter here. Exits 1 if
    you need to act, or the download fails. Another sync refuses to start.
    """
    _fetch("amazon", start, end, profile)


@fetch_app.command("ebay")
def fetch_ebay(
    start: Annotated[
        str | None,
        typer.Option("--start", help="First purchase day, YYYY-MM-DD. Give with --end."),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option("--end", help="Last purchase day, YYYY-MM-DD. Give with --start."),
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", help="The eBay profile's key, if you have several.")
    ] = None,
) -> None:
    """Download an eBay profile's orders and save them, as a sync of that profile.

    Without --start and --end, it fetches from shortly before the newest receipt it has. Chrome
    opens with omsync's browser profile. Sign in to eBay there, then press Enter here. Exits 1 if
    you need to act, or the download fails. Another sync refuses to start.
    """
    _fetch("ebay", start, end, profile)


def _fetch(
    vendor_key: str,
    start: str | None,
    end: str | None,
    profile_key: str | None,
) -> None:
    period = parse_period(start, end, start_flag="--start", end_flag="--end")
    if period is not None and period[1] > date.today():
        fail("--end is in the future")
    with ready_session() as (settings, session):
        chosen = _choose_profile(session, vendor_key, profile_key)
        typer.echo(
            f"Fetching {vendor_key} receipts for {chosen.name} "
            f"into {vendor_folder_path(settings, chosen)}."
        )
        try:
            result = sync_one(
                session,
                settings,
                provider_key=chosen.key,
                vault=None,
                prompt=EnterPrompt(),
                period=None if period is None else DateRange(*period),
            )
        except SyncBusy as exc:
            fail(f"{exc}. Try again when it finishes.")
        except ConnectorError as exc:
            fail(str(exc))
        except KeyboardInterrupt:
            typer.echo("Cancelled. Receipts already saved stay; run the same command to get more.")
            raise typer.Exit(code=130) from None
    detail = f": {result.message}" if result.message else ""
    if result.status is not SyncStatus.succeeded:
        fail(f"sync {result.status.value}{detail}")
    typer.echo(result.message or "No new receipts.")
    typer.echo(f"Match them with: omsync receipts run {vendor_key}")


def _choose_profile(session: Session, vendor_key: str, profile_key: str | None) -> VendorProfile:
    """The vendor's profile to sync: the one named, or the only one. Exits 1 when there is none or
    several."""
    profiles = [p for p in list_vendor_profiles(session) if p.vendor_key == vendor_key]
    if profile_key is not None:
        profiles = [p for p in profiles if p.key == profile_key]
    if len(profiles) == 1:
        return profiles[0]
    if not profiles:
        fail(f"no {vendor_key} profile matches. Add one in Settings, Data sync, or check --profile")
    keys = ", ".join(p.key for p in profiles)
    return fail(f"there are several {vendor_key} profiles ({keys}). Choose one with --profile")


def _scope(report: ServiceReport) -> str:
    if report.start is None or report.end is None:
        return "all history"
    return f"transactions posted {report.start.isoformat()} to {report.end.isoformat()}"


def _print_report(report: ServiceReport) -> None:
    if not report.folder.is_dir():
        typer.echo(f"No folder yet at {report.folder}. Add a provider and sync it first.")
    typer.echo(
        f"Read {report.files_read} files and {report.receipts_read} receipts "
        f"for {report.vendor_key}."
    )
    typer.echo(f"Scope: {_scope(report)}.")
    for status, count in report.counts.items():
        if count:
            typer.echo(f"  {status.replace('_', ' ')}: {count}")
    if report.review:
        typer.echo(f"Review ({len(report.review)}):")
        for line in report.review:
            typer.echo(f"  {line.kind.replace('_', ' '):<22}  {line.text}")
