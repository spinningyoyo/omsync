"""`omsync keywords` and `omsync categorize`: keyword files, payee tests, conflicts, amount rules,
and categorize.

Imports and categorize preview by default. Each writes only with --apply, as one batch. Amount
rules are small edits, so they apply at once and print the batch to undo.
"""

from datetime import date
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.cli.common import fail, ready_session
from omsync.cli.groups import amount_rule_app, app, keywords_app
from omsync.core.batches import BatchError
from omsync.core.money import format_cents
from omsync.ledger import categories_admin as admin
from omsync.ledger.categorize import (
    InvalidKeyword,
    apply_categorize,
    explain,
    find_conflicts,
    plan_categorize,
)
from omsync.ledger.keyword_import import (
    KeywordFileError,
    apply_keyword_import,
    plan_keyword_import,
    read_keyword_file,
)
from omsync.ledger.models import Category

_SCOPE_LABELS = {"same_list": "Same list", "across_lists": "Unique and generic"}
_PREVIEW_ONLY = "Preview only: nothing was written. Run again with --apply to {verb}."


@keywords_app.command("import")
def keywords_import(
    file: Annotated[Path, typer.Argument(help="A JSON file mapping category names to patterns.")],
    list_key: Annotated[
        str, typer.Option("--list", help="unique, generic, or a vendor key such as amazon.")
    ],
    apply: Annotated[
        bool, typer.Option("--apply", help="Write the categories and keywords.")
    ] = False,
) -> None:
    """Preview a keyword file's categories and patterns. With --apply, add them to a list."""
    try:
        mapping, warnings = read_keyword_file(file.expanduser())
    except KeywordFileError as exc:
        fail(str(exc))
    for warning in warnings:
        typer.echo(f"warning: {warning}")

    with ready_session() as (_settings, session):
        try:
            plan = plan_keyword_import(session, mapping, list_key=list_key)
        except ValueError as exc:
            fail(str(exc))
        typer.echo(f"New categories: {plan.new_categories}")
        typer.echo(f"New keywords in {list_key}: {plan.new_keywords}")
        typer.echo(f"Skipped duplicates: {plan.skipped_duplicates}")
        if not plan.preview.changes:
            typer.echo("Nothing to import.")
            return
        if not apply:
            typer.echo(_PREVIEW_ONLY.format(verb="import"))
            return
        try:
            batch = apply_keyword_import(
                session, plan, idempotency_key=f"keywords-import:{uuid4()}"
            )
        except BatchError as exc:
            fail(str(exc))
        typer.echo(f"Imported as batch {batch.id}. Undo with: omsync batches undo {batch.id}")


@keywords_app.command("test")
def keywords_test(
    payee: Annotated[str, typer.Argument(help="Payee text to test, as a statement shows it.")],
    amount: Annotated[
        str | None,
        typer.Option("--amount", help="A transaction amount in dollars, to apply amount rules."),
    ] = None,
) -> None:
    """Show every payee keyword that matches, in precedence order. The first one wins.

    With --amount, also show the amount rule that sends the winner to another category.
    """
    amount_cents = None
    if amount is not None:
        try:
            amount_cents = admin.parse_dollars(amount)
        except ValueError as exc:
            fail(str(exc))
    with ready_session() as (_settings, session):
        try:
            explanation = explain(session, payee, amount_cents)
        except InvalidKeyword as exc:
            fail(str(exc))
        if not explanation.matches:
            typer.echo("No keyword matches. This payee would stay in triage.")
            return
        names = admin.category_names(session)
        rule = explanation.amount_rule
        for position, match in enumerate(explanation.matches):
            mark = "winner " if position == 0 else "       "
            typer.echo(
                f'{mark}{match.list_key:<8} "{match.pattern}" -> '
                f"{names.get(match.category_id, '?')}"
            )
            if position == 0 and rule is not None:
                typer.echo(
                    f"       amount rule: under {format_cents(rule.below_cents)} -> "
                    f"{names.get(rule.to_category_id, '?')}"
                )


@keywords_app.command("amount-rules")
def keywords_amount_rules() -> None:
    """List amount rules: a payee keyword's category, and where small amounts go instead."""
    with ready_session() as (_settings, session):
        rules = admin.amount_rules(session)
    if not rules:
        typer.echo("No amount rules.")
        return
    for rule in rules:
        state = "on" if rule.enabled else "off"
        typer.echo(
            f"{rule.id:>3}  {rule.list_key:<8} {rule.from_category_name} -> "
            f"{rule.to_category_name} when under {format_cents(rule.below_cents)}  [{state}]"
        )


@amount_rule_app.command("add")
def amount_rule_add(
    list_key: Annotated[str, typer.Option("--list", help="unique or generic.")],
    from_name: Annotated[
        str, typer.Option("--from", help="The category the winning keyword assigns.")
    ],
    below: Annotated[
        str, typer.Option("--below", help="The limit in dollars. Amounts under it are redirected.")
    ],
    to_name: Annotated[str, typer.Option("--to", help="The category to assign instead.")],
) -> None:
    """Add a rule: when a keyword in --list assigns --from and the transaction's amount, spent or
    refunded, is under --below, assign --to instead. Run `omsync categorize` to apply it."""
    try:
        limit = admin.parse_dollars(below)
    except ValueError as exc:
        fail(str(exc))
    with ready_session() as (_settings, session):
        from_id = _category_id(session, from_name)
        to_id = _category_id(session, to_name)
        try:
            batch = admin.add_amount_rule(
                session, list_key, from_id, limit, to_id, idempotency_key=f"amount-rule:{uuid4()}"
            )
        except (ValueError, BatchError) as exc:
            fail(str(exc))
        typer.echo(
            f"Added amount rule as batch {batch.id}. Undo with: omsync batches undo {batch.id}"
        )


@amount_rule_app.command("delete")
def amount_rule_delete(
    rule_id: Annotated[
        int, typer.Argument(help="The rule's ID, from `omsync keywords amount-rules`.")
    ],
) -> None:
    """Delete an amount rule. Run `omsync categorize` to move transactions back."""
    with ready_session() as (_settings, session):
        try:
            batch = admin.delete_amount_rule(
                session, rule_id, idempotency_key=f"amount-rule-delete:{uuid4()}"
            )
        except (LookupError, BatchError) as exc:
            fail(str(exc))
        typer.echo(
            f"Deleted amount rule as batch {batch.id}. Undo with: omsync batches undo {batch.id}"
        )


@keywords_app.command("conflicts")
def keywords_conflicts() -> None:
    """List patterns that point at different categories, within a list or across unique/generic."""
    with ready_session() as (_settings, session):
        conflicts = find_conflicts(session)
    if not conflicts:
        typer.echo("No conflicts.")
        return
    for conflict in conflicts:
        typer.echo(f'{_SCOPE_LABELS[conflict.scope]}: "{conflict.pattern}"')
        for member in conflict.members:
            typer.echo(f'  {member.list_key:<8} "{member.pattern}" -> {member.category_name}')


@app.command("categorize")
def categorize(
    account_id: Annotated[
        int | None, typer.Option("--account-id", help="Only transactions in this account.")
    ] = None,
    from_date: Annotated[
        str | None, typer.Option("--from", help="Only on or after this date, YYYY-MM-DD.")
    ] = None,
    to_date: Annotated[
        str | None, typer.Option("--to", help="Only on or before this date, YYYY-MM-DD.")
    ] = None,
    apply: Annotated[
        bool, typer.Option("--apply", help="Write the categories as one batch.")
    ] = False,
) -> None:
    """Set categories from keywords on transactions that are unset or keyword-set."""
    start = _parse_date("--from", from_date)
    end = _parse_date("--to", to_date)
    if start is not None and end is not None and start > end:
        fail("--from is after --to")
    with ready_session() as (_settings, session):
        try:
            plan = plan_categorize(
                session,
                account_ids=None if account_id is None else [account_id],
                start=start,
                end=end,
            )
        except InvalidKeyword as exc:
            fail(str(exc))
        typer.echo(f"Considered {plan.considered} transactions; {plan.changed} would change.")
        for transition in plan.transitions:
            typer.echo(
                f"{_label(transition.from_name)} → {_label(transition.to_name)}: {transition.count}"
            )
        if plan.preview is None:
            typer.echo("Nothing to categorize.")
            return
        if not apply:
            typer.echo(_PREVIEW_ONLY.format(verb="categorize"))
            return
        try:
            batch = apply_categorize(session, plan, idempotency_key=f"categorize:{uuid4()}")
        except BatchError as exc:
            fail(str(exc))
        typer.echo(
            f"Categorized {plan.changed} transactions as batch {batch.id}. "
            f"Undo with: omsync batches undo {batch.id}"
        )


def _category_id(session: Session, name: str) -> int:
    """The id of the category with this name, ignoring case. Exits 1 when there is none."""
    wanted = name.strip().casefold()
    for category_id, existing in session.execute(select(Category.id, Category.name)).all():
        if existing.casefold() == wanted:
            return int(category_id)
    fail(f"no category named {name.strip()!r}")


def _parse_date(option: str, raw: str | None) -> date | None:
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        fail(f"{option} must be a date like 2026-10-01")


def _label(category_name: str | None) -> str:
    return "(unset)" if category_name is None else category_name
