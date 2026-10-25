"""`omsync batches`: list recent batches, or undo one while nothing it changed has moved on."""

from typing import Annotated, Any

import typer
from sqlalchemy import select

from omsync.cli.common import fail, ready_session
from omsync.cli.groups import batches_app
from omsync.core.batch_models import Batch
from omsync.core.batches import BatchError, UndoConflict
from omsync.reconcile.auto import undo_with_automatic


@batches_app.command("list")
def batches_list(
    limit: Annotated[
        int, typer.Option("--limit", min=1, help="How many to show, newest first.")
    ] = 20,
) -> None:
    """List recent batches: id, kind, state, when applied, and a short summary."""
    with ready_session() as (_settings, session):
        batches = session.scalars(select(Batch).order_by(Batch.id.desc()).limit(limit)).all()
        if not batches:
            typer.echo("No batches yet.")
            return
        for batch in batches:
            when = batch.applied_at.strftime("%Y-%m-%d %H:%M UTC") if batch.applied_at else "-"
            typer.echo(
                f"{batch.id:>5}  {batch.kind:<16}  {batch.state.value:<8}  {when:<20}  "
                f"{_summary(batch.summary)}"
            )


@batches_app.command("undo")
def batches_undo(
    batch_id: Annotated[int, typer.Argument(help="The batch to undo, from `omsync batches list`.")],
) -> None:
    """Undo a batch, with the automatic batches it triggered, if nothing they changed has changed
    since and no later batch depends on them."""
    with ready_session() as (_settings, session):
        try:
            batch, followers = undo_with_automatic(session, batch_id)
        except UndoConflict as exc:
            typer.echo(f"error: {exc}", err=True)
            for table, row_id in exc.rows:
                typer.echo(f"  {table} row {row_id}", err=True)
            raise typer.Exit(code=1) from exc
        except BatchError as exc:
            fail(str(exc))
        with_automatic = f", with {followers} automatic batch(es) it triggered" if followers else ""
        typer.echo(f"Undid batch {batch.id} ({batch.kind}){with_automatic}.")


def _summary(summary: dict[str, Any] | None) -> str:
    if not summary:
        return ""
    text = " ".join(f"{key}={value}" for key, value in summary.items())
    return text if len(text) <= 80 else text[:77] + "..."
