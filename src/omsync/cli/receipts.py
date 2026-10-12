"""`omsync receipts`: match a vendor's receipts to your transactions, list the vendors, or download.

`run` previews by default and writes nothing. It covers all history unless --start and --end give
a period, which limits what is planned to charges whose transaction posted inside it. With --apply
it writes the planned charges as one batch, which `omsync batches undo` can reverse. `vendors`
reads only the settings. `fetch costco` downloads receipts through Chrome, and takes the same lock
as `omsync sync`, so the two never drive Chrome at once.
"""

from datetime import date
from typing import Annotated
from uuid import uuid4

import typer

from omsync.cli.common import fail, load_settings_or_exit, parse_date, parse_period, ready_session
from omsync.cli.groups import fetch_app, receipts_app
from omsync.core.batches import BatchError
from omsync.core.locks import SYNC_LOCK_NAME, LockBusy, exclusive_lock
from omsync.ingest.connectors.base import ConnectorError, NeedsUserAction
from omsync.receipts.service import (
    ServiceReport,
    apply_report,
    registered_vendors,
    run_vendor,
    vendor_folder,
)
from omsync.receipts.vendors.costco import RECEIPTS_FOLDER
from omsync.receipts.vendors.costco_fetch import fetch_receipts

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
    """List the receipt vendors omsync reads, and the folder each one reads from."""
    settings = load_settings_or_exit()
    for vendor in registered_vendors():
        folder = vendor_folder(settings, vendor.key)
        state = "found" if folder.is_dir() else "no folder yet"
        typer.echo(f"{vendor.key:<10}  {folder}  ({state})")


class EnterPrompt:
    """Waits for you to press Enter, such as after you sign in to Costco in Chrome."""

    def ask(self, question: str, *, secret: bool = False, digits: int | None = None) -> str:
        return typer.prompt(question, default="", show_default=False, prompt_suffix=" ")


@fetch_app.command("costco")
def fetch_costco(
    start: Annotated[str, typer.Option("--start", help="First purchase day, YYYY-MM-DD.")],
    end: Annotated[
        str, typer.Option("--end", help="Last purchase day, YYYY-MM-DD. Not after today.")
    ],
) -> None:
    """Download Costco receipts for a date range into receipts/COSTCO.

    Chrome must already be running with remote debugging on 127.0.0.1:9222, using omsync's browser
    profile. Sign in to Costco there, then press Enter here. Receipts already on disk are skipped.
    Exits 1 if you need to act, or the download fails. Another sync or download refuses to start.
    """
    first = parse_date("--start", start)
    last = parse_date("--end", end)
    if first > last:
        fail("--start is after --end")
    if last > date.today():
        fail("--end is in the future")
    settings = load_settings_or_exit()
    root = settings.receipts_dir / RECEIPTS_FOLDER
    typer.echo(f"Fetching Costco receipts from {first} to {last} into {root}.")
    try:
        # The sync lock also covers this download, so a bank sync and a Costco fetch never share
        # Chrome. A lock held by either makes the other refuse at once.
        with exclusive_lock(settings.data_dir / SYNC_LOCK_NAME):
            saved = fetch_receipts(settings, EnterPrompt(), start=first, end=last)
    except LockBusy:
        fail("another sync or receipt download is already running. Try again when it finishes.")
    except (NeedsUserAction, ConnectorError) as exc:
        fail(str(exc))
    except KeyboardInterrupt:
        typer.echo("Cancelled. Receipts already saved stay; run the same command to get the rest.")
        raise typer.Exit(code=130) from None
    if not saved:
        typer.echo("No new receipt files were saved for that range.")
        return
    noun = "file" if len(saved) == 1 else "files"
    typer.echo(f"Saved {len(saved)} new receipt {noun} to {root}.")
    typer.echo("Match them with: omsync receipts run costco")


def _scope(report: ServiceReport) -> str:
    if report.start is None or report.end is None:
        return "all history"
    return f"transactions posted {report.start.isoformat()} to {report.end.isoformat()}"


def _print_report(report: ServiceReport) -> None:
    if not report.folder.is_dir():
        typer.echo(f"No folder yet at {report.folder}. Put the vendor's exports there.")
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
