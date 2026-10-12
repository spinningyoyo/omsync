"""Merge one institution into another: move its accounts, aliases and providers, then delete it.

    uv run python scripts/merge_institution.py chase_joint chase           # dry run, no writes
    uv run python scripts/merge_institution.py chase_joint chase --apply   # one undoable batch

Transactions, statement files and sync history follow their accounts and are not touched. The
source's statement folder is not moved: files already imported stay recorded by their contents.
Refuses, writing nothing, when a statement account ID of the source is already used at the target.
"""

import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.cli.common import ready_session
from omsync.core.batches import Change, apply, make_preview, snapshot
from omsync.ledger.models import Account, AccountAlias, Institution, Provider


class MergeError(Exception):
    pass


def _institution(session: Session, key: str) -> Institution:
    found = session.scalars(select(Institution).where(Institution.key == key)).first()
    if found is None:
        raise MergeError(f"there is no institution {key!r}")
    return found


def _moved(session: Session, table: str, row_id: int, target_id: int) -> Change:
    before = snapshot(session, table, row_id)
    assert before is not None
    return Change(table, str(row_id), before, {**before, "institution_id": target_id})


def merge(session: Session, source_key: str, target_key: str, *, apply_it: bool) -> list[str]:
    if source_key == target_key:
        raise MergeError("the source and the target are the same institution")
    source = _institution(session, source_key)
    target = _institution(session, target_key)

    accounts = session.scalars(select(Account).where(Account.institution_id == source.id)).all()
    aliases = session.scalars(
        select(AccountAlias).where(AccountAlias.institution_id == source.id)
    ).all()
    providers = session.scalars(select(Provider).where(Provider.institution_id == source.id)).all()
    taken = set(
        session.scalars(
            select(AccountAlias.statement_account_id).where(
                AccountAlias.institution_id == target.id
            )
        ).all()
    )
    clashes = [alias for alias in aliases if alias.statement_account_id in taken]
    if clashes:
        raise MergeError(
            f"{len(clashes)} statement account ID(s) of {source_key} are already used at "
            f"{target_key}; map them by hand first"
        )

    target_open = {
        account.last4
        for account in session.scalars(
            select(Account).where(Account.institution_id == target.id, Account.closed.is_(False))
        )
        if account.last4
    }
    shared = [
        account
        for account in accounts
        if not account.closed and account.last4 and account.last4 in target_open
    ]
    if shared:
        raise MergeError(
            f"{len(shared)} open account(s) of {source_key} end in the same four digits as an "
            f"open account at {target_key}, and a site would find two. Close one of them first"
        )

    changes = [_moved(session, "account", account.id, target.id) for account in accounts]
    changes += [_moved(session, "account_alias", alias.id, target.id) for alias in aliases]
    changes += [_moved(session, "provider", provider.id, target.id) for provider in providers]
    changes.append(
        Change("institution", str(source.id), snapshot(session, "institution", source.id), None)
    )

    lines = [
        f"{'move' if apply_it else 'would move'} into {target_key} ({target.name}):",
        *[f"  account {account.name} ••{account.last4 or '----'}" for account in accounts],
        f"  {len(aliases)} alias(es)",
        *[f"  provider {provider.key}" for provider in providers],
        f"{'delete' if apply_it else 'would delete'} institution {source_key} ({source.name})",
    ]
    if apply_it:
        preview = make_preview(
            session,
            "institution_merge",
            changes,
            summary={"from": source_key, "into": target_key},
        )
        batch = apply(session, preview, idempotency_key=f"merge:{source_key}:{target_key}")
        lines.append(f"done: batch {batch.id}. Undo with: uv run omsync batches undo {batch.id}")
    else:
        lines.append("dry run only; nothing was written. Add --apply to merge.")
    return lines


def main() -> int:
    args = [arg for arg in sys.argv[1:] if arg != "--apply"]
    if len(args) != 2:
        print("usage: merge_institution.py SOURCE_KEY TARGET_KEY [--apply]", file=sys.stderr)
        return 2
    with ready_session() as (settings, session):
        try:
            lines = merge(session, args[0], args[1], apply_it="--apply" in sys.argv[1:])
        except MergeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print("\n".join(lines))
        folder = settings.statements_dir / args[0].upper()
        if folder.is_dir() and any(folder.iterdir()):
            print(f"note: {folder} still holds files; new syncs use {args[1].upper()}/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
