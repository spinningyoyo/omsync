"""`omsync import`: preview statement files, or with --apply import each one as its own batch.

The loop lives in omsync.ingest.pipeline, which `omsync sync` shares. This module selects the
files the command names, and prints the report.
"""

from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy.orm import Session

from omsync.cli.common import ready_session
from omsync.cli.groups import app
from omsync.core.settings import Settings
from omsync.ingest.dropfolder import PendingFile, location_of, pending_files
from omsync.ingest.pipeline import FileResult, ImportReport, import_files
from omsync.ledger.sources import account_digits

_TYPES = "checking|savings|credit_card|brokerage|other"


@app.command("import")
def import_statements(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(help="Statement files. With none, every pending file in the drop folder."),
    ] = None,
    institution: Annotated[
        str | None,
        typer.Option(
            "--institution", help="Institution key, such as boa. Needed outside the folder."
        ),
    ] = None,
    account: Annotated[
        str | None,
        typer.Option("--account", help="Statement account ID, for formats that lack one."),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Copy outside files into the drop folder and import them."),
    ] = False,
) -> None:
    """Preview how statement files would import. With --apply, import them as batches.

    Without --apply nothing is written: not the database, and not copies of outside files.
    """
    key = institution.strip().lower() if institution else None
    account_id = account.strip() if account else None
    with ready_session() as (settings, session):
        failures = 0
        files, not_files = _select(session, settings, paths or [], key, account_id)
        failures += not_files
        if not files and not failures:
            typer.echo("Nothing to import.")
        report = import_files(session, settings, files, apply=apply)
        echo_import_report(report)
        if not apply and report.planned:
            typer.echo("Preview only: nothing was written. Run again with --apply to import.")
        failures += report.failures
        if failures:
            raise typer.Exit(code=1)


def echo_import_report(report: ImportReport) -> None:
    """Print one line per file, then the rows that need review and the automatic links."""
    for result in report.files:
        _echo_file(result)
    if report.applied and report.review:
        noun = "row needs" if report.review == 1 else "rows need"
        typer.echo(f"{report.review} {noun} review")
    auto = report.auto
    if auto.categorized:
        typer.echo(f"Categorized {auto.categorized} transactions from keywords.")
    if auto.identified:
        typer.echo(f"Identified the counterparty of {auto.identified} transactions.")
    if auto.linked:
        noun = "link" if auto.linked == 1 else "links"
        typer.echo(
            f"Linked {auto.linked} {noun} automatically (batch {auto.batch_id}). "
            f"Undo with: omsync batches undo {auto.batch_id}"
        )
    if auto.problem:
        typer.echo(
            f"Automatic linking did not run: {auto.problem}. Review it on Reconcile.", err=True
        )


def _select(
    session: Session,
    settings: Settings,
    paths: list[Path],
    institution_key: str | None,
    account_id: str | None,
) -> tuple[list[PendingFile], int]:
    """The files to import, and how many named paths were not files. Named paths get the
    institution and account from the flags, or else from their place in the drop folder."""
    if not paths:
        return [
            item
            for item in pending_files(session, settings, institution_key)
            if account_id in (None, item.statement_account_id)
        ], 0
    files: list[PendingFile] = []
    not_files = 0
    for raw in paths:
        path = raw.expanduser().resolve()
        if not path.is_file():
            typer.echo(f"{raw.name}: not a file", err=True)
            not_files += 1
            continue
        location = location_of(settings, path)
        if location is None:
            files.append(PendingFile(institution_key, account_id, path))
        else:
            files.append(
                PendingFile(
                    institution_key or location.institution_key,
                    account_id or location.statement_account_id,
                    path,
                )
            )
    return files, not_files


def _echo_file(result: FileResult) -> None:
    if result.unmapped_institution is not None:
        account_type = result.unmapped_type_hint or _TYPES
        typer.echo(
            f"{result.error}. Add it with:\n"
            f"  omsync accounts add --institution {result.unmapped_institution} "
            '--institution-name "<INSTITUTION NAME>" '
            "--statement-account <STATEMENT_ACCOUNT_ID> "
            f'--name "<ACCOUNT NAME>" --type {account_type}',
            err=True,
        )
    elif result.error is not None:
        typer.echo(result.error, err=True)
    elif result.already_imported:
        typer.echo(f"{result.name}  already imported")
    else:
        digits = account_digits(result.account_last4, result.account_alias_last4) or "••????"
        typer.echo(
            f"{result.name}  account {digits}  "
            f"joined {result.joined}, created {result.created}, review {result.review}"
        )
