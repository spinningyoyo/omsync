"""Statement accounts in the drop folder, for the add-institution wizard.

A statement account is the account ID a statement file carries: an OFX file's ACCTID, or the
account folder of a format with no account number, such as BoA CSV. Discovery reads each pending
file, one not yet imported, with its format, as import does, and groups the readable files by
that ID. It writes nothing.

The ID is kept for matching and is never shown: a DiscoveredAccount displays only its last four
digits, and its ID is left out of repr. A file that cannot be read is listed by name, with any
run of five or more digits masked, because a file can be named after its full account number.
"""

import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from omsync.core.settings import Settings
from omsync.ingest.accounts import find_account, last4_of
from omsync.ingest.dropfolder import pending_files
from omsync.ingest.formats.base import FormatError, Statement
from omsync.ingest.formats.registry import format_for
from omsync.ledger.models import Account, AccountAlias, Institution

_LONG_DIGITS = re.compile(r"\d{5,}")
_MASKED = "****"


def mask_account_numbers(text: str) -> str:
    """Replace each run of five or more digits with the bullets and its last four digits."""
    return _LONG_DIGITS.sub(lambda match: f"••{match.group(0)[-4:]}", text)


@dataclass(frozen=True)
class DiscoveredAccount:
    """One statement account found in pending files at an institution.

    account_type_hint is the account type the files report, or None. file_count counts the readable
    pending files. period_start and period_end span them, or are None when no file gives dates.
    mapped_account_id is the account this statement account maps to, or None if it is unmapped.
    """

    institution_key: str
    statement_account_id: str = field(repr=False)
    account_type_hint: str | None
    file_count: int
    period_start: date | None
    period_end: date | None
    mapped_account_id: int | None

    @property
    def last4(self) -> str:
        """The last four characters for display, or four stars for an ID too short to show."""
        return last4_of(self.statement_account_id)


@dataclass(frozen=True)
class StatementScan:
    """What the drop folder holds for one institution. unreadable has file names only, masked."""

    accounts: tuple[DiscoveredAccount, ...]
    unreadable: tuple[str, ...]


@dataclass
class _Group:
    file_count: int = 0
    type_hint: str | None = None
    period_start: date | None = None
    period_end: date | None = None

    def add(self, statement: Statement) -> None:
        self.file_count += 1
        if self.type_hint is None:
            self.type_hint = statement.account_type_hint
        if statement.period_start is not None and (
            self.period_start is None or statement.period_start < self.period_start
        ):
            self.period_start = statement.period_start
        if statement.period_end is not None and (
            self.period_end is None or statement.period_end > self.period_end
        ):
            self.period_end = statement.period_end


def scan_statement_files(
    session: Session, settings: Settings, institution_key: str
) -> StatementScan:
    """Group the institution's pending files by statement account, and list the unreadable ones.

    Accounts come unmapped first, then mapped, each in ID order. Read-only.
    """
    groups: dict[str, _Group] = {}
    unreadable: list[str] = []
    for pending in pending_files(session, settings, institution_key):
        try:
            statement_format = format_for(institution_key, pending.path)
            statement = statement_format.read(
                pending.path, statement_account_id=pending.statement_account_id
            )
        except (FormatError, OSError):
            unreadable.append(mask_account_numbers(pending.path.name))
            continue
        groups.setdefault(statement.statement_account_id, _Group()).add(statement)

    accounts: list[DiscoveredAccount] = []
    for statement_account_id, group in groups.items():
        mapped = find_account(session, institution_key, statement_account_id)
        accounts.append(
            DiscoveredAccount(
                institution_key=institution_key,
                statement_account_id=statement_account_id,
                account_type_hint=group.type_hint,
                file_count=group.file_count,
                period_start=group.period_start,
                period_end=group.period_end,
                mapped_account_id=None if mapped is None else mapped.id,
            )
        )
    accounts.sort(
        key=lambda account: (account.mapped_account_id is not None, account.statement_account_id)
    )
    return StatementScan(accounts=tuple(accounts), unreadable=tuple(sorted(unreadable)))


def discover_statement_accounts(
    session: Session, settings: Settings, institution_key: str
) -> list[DiscoveredAccount]:
    """The statement accounts in the institution's pending files. See scan_statement_files."""
    return list(scan_statement_files(session, settings, institution_key).accounts)


def suggested_account(session: Session, account: DiscoveredAccount) -> Account | None:
    """The open account at this institution that ends in the statement account's four digits, when
    exactly one does. An account ends in its displayed last four, and in the digits of an alias that
    is only those four, as one added for a reissued card before its first file. The wizard
    preselects it as "Same as existing account", which adds the alias, for a reissued card, or for
    an account made before its file arrived. None when there is no match or more than one, so
    nothing is guessed. Read-only."""
    if account.mapped_account_id is not None or account.last4 == _MASKED:
        return None
    digits_alias = (
        select(AccountAlias.id)
        .where(
            AccountAlias.account_id == Account.id,
            AccountAlias.statement_account_id == account.last4,
        )
        .exists()
    )
    matches = session.scalars(
        select(Account)
        .join(Institution, Institution.id == Account.institution_id)
        .where(
            Institution.key == account.institution_key,
            Account.closed.is_(False),
            or_(Account.last4 == account.last4, digits_alias),
        )
    ).all()
    return matches[0] if len(matches) == 1 else None
