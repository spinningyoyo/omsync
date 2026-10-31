"""The connector protocol: how omsync fetches statement files from an institution.

A connector only downloads. It writes statement files into `dest` and returns their paths.
Reading, matching, and importing them is the job of the statement formats and the import
pipeline, whatever fetched the file, so a connector never imports anything.

ConnectorError and NeedsUserAction messages are stored in the sync log and shown to you. They
must never contain a password, a token, or an account number.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from omsync.core.vault import Vault


class ConnectorError(Exception):
    """A connector could not fetch statement files. The message is safe to store and show."""


class NeedsUserAction(ConnectorError):
    """A connector needs you: an MFA code was rejected, or a login has expired."""


class PartialFetch(ConnectorError):
    """A fetch that got files for some accounts and not for others. files are what it got, already
    stored. The message says which accounts failed, and never holds the bank's own text."""

    def __init__(self, message: str, files: list[Path]) -> None:
        super().__init__(message)
        self.files = files


class Prompt(Protocol):
    """Asks you a question while a sync runs, such as for an MFA code."""

    def ask(self, question: str, *, secret: bool = False, digits: int | None = None) -> str:
        """Return your answer. With secret=True, the answer is not echoed as you type it. digits is
        the number of digits a one-time code has, when that is known, so a screen can send the
        answer as soon as it is complete."""
        ...


@dataclass(frozen=True)
class ConnectorAccount:
    """An account to fetch for.

    account_id is the omsync account. last4 is its ••last4 from Account.last4, which is "" for an
    account with none. statement_account_id is the ID the institution's files carry for it, taken
    from the account's current alias (see ledger.sources.current_alias_id), or None when the
    account has no alias yet. type is the account type, such as "checking".

    A connector selects the account on the bank's site by statement_account_id when there is one,
    and by last4 otherwise (see banks.base.site_last4).
    """

    account_id: int
    last4: str
    type: str
    statement_account_id: str | None


@dataclass(frozen=True)
class DateRange:
    """The first and last day to fetch, both inclusive."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("a date range cannot start after it ends")


class Connector(Protocol):
    """One way of fetching statement files. key is stored in provider; label is shown.

    last4_is_statement_id is True for an institution whose files name each account by its last
    four digits, such as BoA's CSV folders. An account created for such a connector gets an alias
    equal to its last four digits, so its files import without a second step.
    """

    key: str
    label: str
    last4_is_statement_id: bool

    def fetch(
        self,
        accounts: list[ConnectorAccount],
        period: DateRange,
        vault: Vault,
        prompt: Prompt,
        dest: Path,
        *,
        vault_item_id: str | None,
        close_on_failure: bool = True,
    ) -> list[Path]:
        """Download statement files for these accounts and period, and return their paths.

        dest is statements/<INSTITUTION>/, and account subfolders go under it. Write nothing
        outside dest, and never import. Return only the files this call fetched: the sync
        imports exactly those. vault_item_id names the vault login for this institution, or is
        None if none is configured. Raise NeedsUserAction when a person must act, and
        ConnectorError for any other failure. A browser connector closes its browser when it fails,
        unless close_on_failure is False, which leaves it open so the person can look at the page.
        """
        ...
