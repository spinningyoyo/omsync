"""Sync: fetch a provider's statement files with its connector, then import the new ones.

A sync runs one at a time on this machine. It holds an exclusive lock on <data_dir>/sync.lock for
its whole length, the import included, and a second sync raises SyncBusy instead of waiting.
sync_all holds the lock across all its providers.

Each sync writes one SyncRun (ledger/sync_models.py): running when it starts, then succeeded,
failed, needs_user_action, or cancelled. A connector's exceptions become that status and never
escape. A sync always imports the files it fetched, each as its own batch, then categorizes the
provider's accounts with the keywords. A sync cancelled while it fetches imports nothing.
"""

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.core.batches import BatchError
from omsync.core.locks import SYNC_LOCK_NAME, LockBusy, exclusive_lock
from omsync.core.settings import Settings
from omsync.core.vault import Vault, VaultError, VaultLocked
from omsync.ingest.accounts import AccountError, add_alias
from omsync.ingest.connectors.base import (
    Connector,
    ConnectorAccount,
    ConnectorError,
    DateRange,
    NeedsUserAction,
    PartialFetch,
    Prompt,
    SyncCancelled,
)
from omsync.ingest.connectors.config import enabled_provider_keys
from omsync.ingest.connectors.registry import get_connector
from omsync.ingest.discovery import scan_statement_files, suggested_account
from omsync.ingest.dropfolder import PendingFile, location_of
from omsync.ingest.pipeline import ImportReport, import_files
from omsync.ingest.timing import timed
from omsync.ledger.categorize import InvalidKeyword, apply_categorize, plan_categorize
from omsync.ledger.models import (
    Account,
    AccountAlias,
    AccountType,
    Institution,
    Provider,
    ProviderAccount,
    SyncRun,
    SyncStatus,
    Transaction,
)
from omsync.ledger.other_side import apply_other_side, plan_other_side
from omsync.ledger.sources import current_alias_id

LOOKBACK = timedelta(days=45)
MESSAGE_LIMIT = 200
_INSTITUTION_KEY = re.compile(r"[a-z0-9][a-z0-9_-]*")


class SyncBusy(Exception):
    """Another sync is running on this machine. Nothing was fetched or written."""


@dataclass(frozen=True)
class SyncResult:
    """The outcome of one sync. report is None when the connector fetched nothing to import,
    which is the case for every failed fetch. message is safe to show and is also in the log.
    linked has the last four digits of each statement account this sync linked to an account.
    categorized is how many transactions the keywords categorized or sent back to triage."""

    run_id: int
    status: SyncStatus
    period: DateRange
    files_fetched: int
    message: str | None
    report: ImportReport | None
    linked: tuple[str, ...] = ()
    categorized: int = 0

    @property
    def unlinked(self) -> tuple[str, ...]:
        """The last four digits of each statement account that no account is linked to, so its
        files could not be imported."""
        if self.report is None:
            return ()
        return tuple(sorted({f.unmapped_last4 for f in self.report.files if f.unmapped_last4}))


@dataclass(frozen=True)
class ProviderSync:
    """What sync_all did for one provider. A provider that could not run at all, such as one with
    no accounts, writes no SyncRun, so its error is set instead of result."""

    provider_key: str
    result: SyncResult | None
    error: str | None


ProgressCallback = Callable[[str, ProviderSync | None], None]
"""Called as sync_all reaches each provider: (key, None) when its sync starts, then (key, outcome)
when it ends, with the outcome sync_all records for it."""


@dataclass(frozen=True)
class _Plan:
    """What a sync needs from the database, read before its SyncRun is written."""

    provider_key: str
    institution_key: str
    connector: Connector
    accounts: list[ConnectorAccount]
    vault_item_id: str | None


def sync_provider(
    session: Session,
    settings: Settings,
    *,
    provider_key: str,
    vault: Vault,
    prompt: Prompt,
    period: DateRange | None = None,
) -> SyncResult:
    """Fetch a provider's statement files for its accounts, then import the newly fetched ones.

    The period defaults to 45 days before the latest transaction in those accounts, or to January 1
    of this year when there is none, through today. Raises SyncBusy if another sync holds the lock,
    and ConnectorError if the provider is unknown, turned off, or has no open accounts. Connector
    failures do not raise: they set the result's status.
    """
    with _sync_lock(settings.data_dir / SYNC_LOCK_NAME):
        return _sync(
            session,
            settings,
            provider_key=provider_key,
            vault=vault,
            prompt=prompt,
            period=period,
        )


def sync_all(
    session: Session,
    settings: Settings,
    *,
    vault: Vault,
    prompt: Prompt,
    period: DateRange | None = None,
    on_progress: ProgressCallback | None = None,
) -> list[ProviderSync]:
    """Sync every enabled provider, one after another, in institution key then provider key order,
    each for the given period (see sync_provider).

    A provider that fails, or cannot run, does not stop the others. Cancelling through the prompt
    does: no provider starts after it, so only the providers that started have an entry in the
    list. Raises SyncBusy, before anything is fetched, if another sync holds the lock.
    """
    outcomes: list[ProviderSync] = []
    with _sync_lock(settings.data_dir / SYNC_LOCK_NAME):
        for key in enabled_provider_keys(session):
            if prompt.cancelled():
                break
            if on_progress is not None:
                on_progress(key, None)
            outcome = _sync_one(
                session,
                settings,
                key,
                vault=vault,
                prompt=prompt,
                period=period,
            )
            outcomes.append(outcome)
            if on_progress is not None:
                on_progress(key, outcome)
    return outcomes


def _sync_one(
    session: Session,
    settings: Settings,
    key: str,
    *,
    vault: Vault,
    prompt: Prompt,
    period: DateRange | None,
) -> ProviderSync:
    try:
        result = _sync(
            session,
            settings,
            provider_key=key,
            vault=vault,
            prompt=prompt,
            period=period,
        )
    except ConnectorError as exc:
        return ProviderSync(key, None, str(exc))
    except Exception as exc:  # one provider's crash must not stop the rest
        session.rollback()
        return ProviderSync(key, None, f"unexpected error ({type(exc).__name__})")
    return ProviderSync(key, result, None)


def _sync(
    session: Session,
    settings: Settings,
    *,
    provider_key: str,
    vault: Vault,
    prompt: Prompt,
    period: DateRange | None,
) -> SyncResult:
    plan = _prepare(session, provider_key)
    window = period or _default_period(
        session, [account.account_id for account in plan.accounts], date.today()
    )
    run = SyncRun(
        institution_key=plan.institution_key,
        provider_key=plan.provider_key,
        connector_key=plan.connector.key,
        started_at=_utc_now(),
        status=SyncStatus.running,
        files_fetched=0,
    )
    session.add(run)
    session.commit()
    return _run(
        session,
        settings,
        run,
        plan,
        window=window,
        vault=vault,
        prompt=prompt,
    )


def _run(
    session: Session,
    settings: Settings,
    run: SyncRun,
    plan: _Plan,
    *,
    window: DateRange,
    vault: Vault,
    prompt: Prompt,
) -> SyncResult:
    partial: PartialFetch | None = None
    try:
        fetched = plan.connector.fetch(
            plan.accounts,
            window,
            vault,
            prompt,
            _institution_dir(settings, plan.institution_key),
            vault_item_id=plan.vault_item_id,
        )
    except PartialFetch as exc:
        # Some accounts failed and some did not: what was fetched is imported, then reported.
        fetched, partial = exc.files, exc
    except Exception as exc:  # a connector must never crash the caller; its error is the status
        status, message = _fetch_failure(exc)
        return _finish(session, run, status, 0, message, window)

    try:
        pending = _pending_files(settings, plan.institution_key, fetched)
    except ConnectorError as exc:
        return _finish(session, run, SyncStatus.failed, len(fetched), str(exc), window)

    linked = _link_statement_accounts(session, settings, plan)
    try:
        with timed(plan.provider_key, "import"):
            report = import_files(session, settings, pending, apply=True)
    except BaseException as exc:
        session.rollback()
        _finish(
            session,
            run,
            SyncStatus.failed,
            len(fetched),
            f"stopped unexpectedly ({type(exc).__name__})",
            window,
        )
        raise

    problems: list[str] = [] if partial is None else [str(partial)]
    if report.failures:
        noun = "file could not" if report.failures == 1 else "files could not"
        problem = f"{report.failures} {noun} be imported"
        unlinked = sorted({f.unmapped_last4 for f in report.files if f.unmapped_last4})
        if unlinked:
            digits = ", ".join(f"••{last4}" for last4 in unlinked)
            problem += f". No account is linked to the statement account {digits} yet"
        problems.append(problem)
    with timed(plan.provider_key, "categorize"):
        categorized, categorize_problem = _categorize(session, plan)
    categorized += report.auto.categorized
    if categorize_problem is not None:
        problems.append(categorize_problem)
    status = SyncStatus.failed if problems else SyncStatus.succeeded
    message = "; ".join(problems) or None
    return _finish(
        session,
        run,
        status,
        len(fetched),
        message,
        window,
        report=report,
        linked=linked,
        categorized=categorized,
    )


def _categorize(session: Session, plan: _Plan) -> tuple[int, str | None]:
    """Categorize the provider's accounts as one batch, after identifying the other side as the
    automatic step does, because a rule can read it. Returns the transactions categorized,
    and a problem when the keywords could not be applied. The imported statements stay either
    way."""
    try:
        identifying = plan_other_side(session)
        if identifying.preview is not None:
            apply_other_side(session, identifying, idempotency_key=f"sync-other-side:{uuid4()}")
        categorizing = plan_categorize(
            session, account_ids=[account.account_id for account in plan.accounts]
        )
        if categorizing.preview is None:
            return 0, None
        apply_categorize(session, categorizing, idempotency_key=f"sync-categorize:{uuid4()}")
    except (InvalidKeyword, BatchError) as exc:
        session.rollback()
        return 0, f"imported, but categorizing failed: {exc}"
    return categorizing.changed, None


def _link_statement_accounts(
    session: Session, settings: Settings, plan: "_Plan"
) -> tuple[str, ...]:
    """Link each statement account in the institution's pending files to the provider's account
    that ends in the same four digits, so the files can import without a manual step.

    A link is made only when exactly one open account at the institution ends in those digits, it
    is one this provider fetches, and the provider asked the bank for it by exactly those digits:
    it has no link yet, or its current alias is only those four, as the person adds for a reissued
    card. The file it got back is that account's. A reissued card with no such alias, or digits
    that two accounts share, is left for the person to link. Returns the digits linked.
    """
    mine = {account.account_id: account for account in plan.accounts}
    linked: list[str] = []
    for discovered in scan_statement_files(session, settings, plan.institution_key).accounts:
        match = suggested_account(session, discovered)
        account = None if match is None else mine.get(match.id)
        if account is None or account.statement_account_id not in (None, discovered.last4):
            continue
        try:
            add_alias(
                session,
                account_id=account.account_id,
                institution_key=plan.institution_key,
                statement_account_id=discovered.statement_account_id,
                idempotency_key=uuid4().hex,
            )
        except (AccountError, BatchError):
            session.rollback()
            continue
        linked.append(discovered.last4)
    return tuple(linked)


def _prepare(session: Session, provider_key: str) -> _Plan:
    row = session.execute(
        select(Provider, Institution.key)
        .join(Institution, Institution.id == Provider.institution_id)
        .where(Provider.key == provider_key)
    ).first()
    if row is None:
        raise ConnectorError(f"there is no provider {provider_key!r}")
    provider, institution_key = row
    if not provider.enabled:
        raise ConnectorError(f"the provider {provider_key} is turned off")
    accounts = _connector_accounts(session, provider.id)
    if not accounts:
        raise ConnectorError(f"{provider_key} has no open accounts to sync; add accounts first")
    return _Plan(
        provider_key=provider_key,
        institution_key=institution_key,
        connector=get_connector(provider.connector_key),
        accounts=accounts,
        vault_item_id=provider.vault_item_id,
    )


def _connector_accounts(session: Session, provider_id: int) -> list[ConnectorAccount]:
    """One entry per open account the provider fetches. last4 is the account's own, and
    statement_account_id is its current alias (see current_alias_id), or None while it has none.
    An account is not left out for lacking an alias, so a bank can be synced before any statement
    file names it."""
    current = (
        select(AccountAlias.statement_account_id)
        .where(AccountAlias.id == current_alias_id(Account.id))
        .correlate_except(AccountAlias)
        .scalar_subquery()
    )
    accounts = session.execute(
        select(Account.id, Account.last4, Account.type, current)
        .join(ProviderAccount, ProviderAccount.account_id == Account.id)
        .where(ProviderAccount.provider_id == provider_id, Account.closed.is_(False))
        .order_by(Account.id)
    ).all()
    return [
        ConnectorAccount(
            account_id=account_id,
            last4=last4 or "",
            type=AccountType(account_type).value,
            statement_account_id=statement_account_id,
        )
        for account_id, last4, account_type, statement_account_id in accounts
    ]


def _default_period(session: Session, account_ids: list[int], today: date) -> DateRange:
    latest = session.scalar(
        select(func.max(Transaction.posted_on)).where(Transaction.account_id.in_(account_ids))
    )
    start = date(today.year, 1, 1) if latest is None else latest - LOOKBACK
    return DateRange(start=min(start, today), end=today)


def _pending_files(
    settings: Settings, institution_key: str, fetched: list[Path]
) -> list[PendingFile]:
    """The fetched files as drop-folder locations. A file outside this institution is refused."""
    pending: list[PendingFile] = []
    for path in fetched:
        location = location_of(settings, path)
        if location is None or location.institution_key != institution_key:
            raise ConnectorError("the connector wrote a file outside the institution's folder")
        pending.append(location)
    return pending


def _finish(
    session: Session,
    run: SyncRun,
    status: SyncStatus,
    files_fetched: int,
    message: str | None,
    window: DateRange,
    *,
    report: ImportReport | None = None,
    linked: tuple[str, ...] = (),
    categorized: int = 0,
) -> SyncResult:
    run.status = status
    run.finished_at = _utc_now()
    run.files_fetched = files_fetched
    run.message = _clip(message)
    session.commit()
    return SyncResult(
        run_id=run.id,
        status=status,
        period=window,
        files_fetched=files_fetched,
        message=run.message,
        report=report,
        linked=linked,
        categorized=categorized,
    )


def _fetch_failure(exc: Exception) -> tuple[SyncStatus, str]:
    if isinstance(exc, SyncCancelled):
        return SyncStatus.cancelled, str(exc)
    if isinstance(exc, NeedsUserAction):
        return SyncStatus.needs_user_action, _clip(str(exc)) or "the connector needs you"
    if isinstance(exc, VaultLocked):
        return SyncStatus.needs_user_action, "the vault is locked; unlock it and sync again"
    if isinstance(exc, ConnectorError | VaultError):
        return SyncStatus.failed, _clip(str(exc)) or "the connector failed"
    # The exception text of an unexpected error can hold anything, so only its type is kept.
    return SyncStatus.failed, f"unexpected error ({type(exc).__name__})"


def _institution_dir(settings: Settings, institution_key: str) -> Path:
    """The institution's folder in the drop folder: the existing one, whatever its case, or a new
    one named in capitals. Mirrors the drop folder's own naming rule."""
    if not _INSTITUTION_KEY.fullmatch(institution_key):
        raise ConnectorError(f"institution key {institution_key!r} cannot name a folder")
    root = settings.statements_dir
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir() and child.name.lower() == institution_key:
                return child
    return root / institution_key.upper()


@contextmanager
def _sync_lock(path: Path) -> Iterator[None]:
    """The sync lock, as SyncBusy when another sync or a Costco download holds it."""
    try:
        with exclusive_lock(path):
            yield
    except LockBusy:
        raise SyncBusy("another sync is already running, or a Costco receipt download") from None


def _clip(text: str | None) -> str | None:
    if text is None:
        return None
    clipped = text.strip()[:MESSAGE_LIMIT]
    return clipped or None


def _utc_now() -> datetime:
    # Naive UTC, to match the other timestamps in the database.
    return datetime.now(UTC).replace(tzinfo=None)
