"""`omsync keywords`, `omsync rules`, and `omsync categorize`: keyword files, payee tests,
conflicts, rules, and categorize.

Imports and categorize preview by default. Each writes only with --apply, as one batch. Rules are
small edits, so they apply at once and print the batch to undo.
"""

from datetime import date
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.cli.common import fail, ready_session
from omsync.cli.groups import app, keywords_app, rules_app
from omsync.core.batches import BatchError
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
from omsync.ledger.models import Account, Category, Contact, RuleOtherSide

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
        typer.Option("--amount", help="A transaction amount in dollars, to apply rules."),
    ] = None,
    person: Annotated[
        bool,
        typer.Option(
            "--person",
            help="Treat the payment as one with a person, so rules limited to a person apply.",
        ),
    ] = False,
) -> None:
    """Show every payee keyword that matches, in precedence order. The first one wins.

    With --amount, also show the rule that decides the category, if one holds. Its conditions
    about the other side or the account cannot be told from a payee, so those rules do not hold.
    """
    amount_cents = None
    if amount is not None:
        try:
            amount_cents = admin.parse_dollars(amount)
        except ValueError as exc:
            fail(str(exc))
    with ready_session() as (_settings, session):
        try:
            explanation = explain(session, payee, amount_cents, with_person=person)
        except InvalidKeyword as exc:
            fail(str(exc))
        if not explanation.matches and explanation.rule is None:
            typer.echo("No keyword matches. This payee would stay in triage.")
            return
        names = admin.category_names(session)
        for position, match in enumerate(explanation.matches):
            mark = "winner " if position == 0 else "       "
            typer.echo(
                f'{mark}{match.list_key:<8} "{match.pattern}" -> '
                f"{names.get(match.category_id, '?')}"
            )
        if explanation.rule is not None:
            row = admin.get_rule(session, explanation.rule.id)
            typer.echo(f"set by rule {row.id}: {row.sentence}")


@rules_app.command("list")
def rules_list() -> None:
    """List rules in order. The first rule that holds sets its category; off rules are skipped."""
    with ready_session() as (_settings, session):
        rules = admin.rule_rows(session)
    if not rules:
        typer.echo("No rules.")
        return
    for rule in rules:
        state = "on" if rule.enabled else "off"
        typer.echo(f"{rule.id:>3}  {rule.sentence}  [{state}]")


@rules_app.command("add")
def rules_add(
    to_name: Annotated[str, typer.Option("--to", help="The category to set when it holds.")],
    from_name: Annotated[
        str | None,
        typer.Option("--from", help="Only when the keywords assign this category."),
    ] = None,
    list_key: Annotated[
        str | None,
        typer.Option("--list", help="With --from: only when the keyword is from this list."),
    ] = None,
    below: Annotated[
        str | None,
        typer.Option("--below", help="Only when the amount, spent or refunded, is under this."),
    ] = None,
    at_least: Annotated[
        str | None,
        typer.Option(
            "--at-least", help="Only when the amount, spent or refunded, is this or more."
        ),
    ] = None,
    other_side: Annotated[
        RuleOtherSide | None,
        typer.Option(
            "--other-side",
            help="person, contact (with --contact), or owner_account: who is on the other side.",
        ),
    ] = None,
    contact_name: Annotated[
        str | None,
        typer.Option("--contact", help="With --other-side contact: the contact's name."),
    ] = None,
    account_name: Annotated[
        str | None,
        typer.Option("--account", help="Only for transactions in this account, by its name."),
    ] = None,
) -> None:
    """Add a rule at the end of the order. Run `omsync categorize` to apply it. Amounts are in
    dollars. Give at least one condition: --from, --below, --at-least, --other-side, or --account.
    """
    with ready_session() as (_settings, session):
        try:
            draft = _draft(
                session,
                to_name=to_name,
                from_name=from_name,
                list_key=list_key,
                below=below,
                at_least=at_least,
                other_side=other_side,
                contact_name=contact_name,
                account_name=account_name,
            )
            batch = admin.add_rule(session, draft, idempotency_key=f"rule:{uuid4()}")
        except (ValueError, LookupError, BatchError) as exc:
            fail(str(exc))
        typer.echo(f"Added rule as batch {batch.id}. Undo with: omsync batches undo {batch.id}")


@rules_app.command("edit")
def rules_edit(
    rule_id: Annotated[int, typer.Argument(help="The rule's ID, from `omsync rules list`.")],
    to_name: Annotated[str, typer.Option("--to", help="The category to set when it holds.")],
    from_name: Annotated[
        str | None,
        typer.Option("--from", help="Only when the keywords assign this category."),
    ] = None,
    list_key: Annotated[
        str | None,
        typer.Option("--list", help="With --from: only when the keyword is from this list."),
    ] = None,
    below: Annotated[
        str | None,
        typer.Option("--below", help="Only when the amount, spent or refunded, is under this."),
    ] = None,
    at_least: Annotated[
        str | None,
        typer.Option(
            "--at-least", help="Only when the amount, spent or refunded, is this or more."
        ),
    ] = None,
    other_side: Annotated[
        RuleOtherSide | None,
        typer.Option(
            "--other-side",
            help="person, contact (with --contact), or owner_account: who is on the other side.",
        ),
    ] = None,
    contact_name: Annotated[
        str | None,
        typer.Option("--contact", help="With --other-side contact: the contact's name."),
    ] = None,
    account_name: Annotated[
        str | None,
        typer.Option("--account", help="Only for transactions in this account, by its name."),
    ] = None,
) -> None:
    """Replace a rule's conditions and category. Its place in the order stays as it is. Every
    condition the rule keeps must be given again: an option left out is cleared."""
    with ready_session() as (_settings, session):
        try:
            draft = _draft(
                session,
                to_name=to_name,
                from_name=from_name,
                list_key=list_key,
                below=below,
                at_least=at_least,
                other_side=other_side,
                contact_name=contact_name,
                account_name=account_name,
            )
            batch = admin.update_rule(
                session, rule_id, draft, idempotency_key=f"rule-edit:{uuid4()}"
            )
        except admin.NothingChanged as exc:
            typer.echo(str(exc))
            return
        except (ValueError, LookupError, BatchError) as exc:
            fail(str(exc))
        typer.echo(f"Edited rule as batch {batch.id}. Undo with: omsync batches undo {batch.id}")


@rules_app.command("enable")
def rules_enable(rule_id: Annotated[int, typer.Argument(help="The rule's ID.")]) -> None:
    """Turn a rule on. Run `omsync categorize` to apply it."""
    _set_enabled(rule_id, enabled=True)


@rules_app.command("disable")
def rules_disable(rule_id: Annotated[int, typer.Argument(help="The rule's ID.")]) -> None:
    """Turn a rule off, so categorize skips it. Run `omsync categorize` to move transactions."""
    _set_enabled(rule_id, enabled=False)


@rules_app.command("move")
def rules_move(
    rule_id: Annotated[int, typer.Argument(help="The rule's ID.")],
    direction: Annotated[str, typer.Argument(help="up (tried sooner) or down (tried later).")],
) -> None:
    """Move a rule one place in the order. The first rule that holds wins, so order matters."""
    with ready_session() as (_settings, session):
        try:
            batch = admin.move_rule(
                session, rule_id, direction, idempotency_key=f"rule-move:{uuid4()}"
            )
        except admin.NothingChanged as exc:
            typer.echo(str(exc))
            return
        except (ValueError, LookupError, BatchError) as exc:
            fail(str(exc))
        typer.echo(f"Moved rule as batch {batch.id}. Undo with: omsync batches undo {batch.id}")


@rules_app.command("remove")
def rules_remove(rule_id: Annotated[int, typer.Argument(help="The rule's ID.")]) -> None:
    """Remove a rule. Run `omsync categorize` to move the transactions it set."""
    with ready_session() as (_settings, session):
        try:
            batch = admin.delete_rule(session, rule_id, idempotency_key=f"rule-delete:{uuid4()}")
        except (LookupError, BatchError) as exc:
            fail(str(exc))
        typer.echo(f"Removed rule as batch {batch.id}. Undo with: omsync batches undo {batch.id}")


def _set_enabled(rule_id: int, *, enabled: bool) -> None:
    with ready_session() as (_settings, session):
        try:
            batch = admin.set_rule_enabled(
                session, rule_id, enabled, idempotency_key=f"rule-enable:{uuid4()}"
            )
        except admin.NothingChanged as exc:
            typer.echo(str(exc))
            return
        except (LookupError, BatchError) as exc:
            fail(str(exc))
        typer.echo(f"Changed rule as batch {batch.id}. Undo with: omsync batches undo {batch.id}")


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
    """Set categories from keywords and rules on transactions that are unset, or set by a keyword
    or a rule."""
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
            if transition.from_name == transition.to_name:
                line = f"{transition.to_name}, now set by {transition.origin.value}"
            else:
                line = f"{_label(transition.from_name)} → {_label(transition.to_name)}"
            typer.echo(f"{line}: {transition.count}")
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


def _draft(
    session: Session,
    *,
    to_name: str,
    from_name: str | None,
    list_key: str | None,
    below: str | None,
    at_least: str | None,
    other_side: RuleOtherSide | None,
    contact_name: str | None,
    account_name: str | None,
) -> admin.RuleDraft:
    """The rule a rules command describes. Names become IDs here; amounts are dollars."""
    return admin.RuleDraft(
        to_category_id=_category_id(session, to_name),
        from_category_id=None if from_name is None else _category_id(session, from_name),
        list_key=list_key,
        below_cents=None if below is None else admin.parse_dollars(below),
        at_least_cents=None if at_least is None else admin.parse_dollars(at_least),
        other_side=other_side,
        contact_id=None if contact_name is None else _named_id(session, Contact, contact_name),
        account_id=None if account_name is None else _named_id(session, Account, account_name),
    )


def _category_id(session: Session, name: str) -> int:
    """The id of the category with this name, ignoring case. Exits 1 when there is none."""
    return _named_id(session, Category, name)


def _named_id(
    session: Session, model: type[Category] | type[Contact] | type[Account], name: str
) -> int:
    """The id of the row of this model with this name, ignoring case. Exits 1 when there is none."""
    wanted = name.strip().casefold()
    for row_id, existing in session.execute(select(model.id, model.name)).all():
        if existing.casefold() == wanted:
            return int(row_id)
    noun = {Category: "category", Contact: "contact", Account: "account"}[model]
    fail(f"no {noun} named {name.strip()!r}")


def _parse_date(option: str, raw: str | None) -> date | None:
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        fail(f"{option} must be a date like 2026-10-01")


def _label(category_name: str | None) -> str:
    return "(unset)" if category_name is None else category_name
