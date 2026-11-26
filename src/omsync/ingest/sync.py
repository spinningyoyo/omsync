"""Bank sync: fetch a bank provider's statement files with its connector, then import the new ones.

omsync.sync.run calls bank_sync while it holds the sync lock, for one provider or for all of them.

Each sync writes one SyncRun (ingest/runs.py): running when it starts, then succeeded,
failed, needs_user_action, or cancelled. A connector's exceptions become that status and never
escape. A sync always imports the files it fetched, each as its own batch, then categorizes the
provider's accounts with the keywords. A sync cancelled while it fetches imports nothing.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.core.batches import BatchError
from omsync.core.settings import Settings
from omsync.core.vault import Vault, VaultError
from omsync.ingest.connectors.base import (
    Connector,
    ConnectorAccount,
    ConnectorError,
    DateRange,
    PartialFetch,
    Prompt,
)
from omsync.ingest.connectors.registry import fetching_connector
from omsync.ingest.discovery import scan_statement_files, suggested_account
from omsync.ingest.dropfolder import PendingFile, location_of
from omsync.ingest.pipeline import ImportReport, import_files
from omsync.ingest.runs import failure_of, finish_run, start_run
from omsync.ingest.timing import timed
from omsync.ledger.accounts import AccountError, add_alias
from omsync.ledger.categorize import InvalidKeyword, apply_categorize, plan_categorize
from omsync.ledger.counterparty import apply_counterparty, plan_counterparty
from omsync.ledger.models import (
    Account,
    AccountAlias,
    AccountType,
    Institution,
    Provider,
    ProviderAccount,
    ProviderKind,
    Transaction,
)
from omsync.ledger.sources import current_alias_id
from omsync.ledger.sync_runs import SyncRun, SyncStatus

logger = logging.getLogger(__name__)

LOOKBACK = timedelta(days=45)
_INSTITUTION_KEY = re.compile(r"[a-z0-9][a-z0-9_-]*")


@dataclass(frozen=True)
class SyncResult:
    """The outcome of one sync. report is None when the connector fetched nothing to import,
    which is the case for every failed fetch. message is safe to show and is also in the log.
    linked has the last four digits of each statement account this sync linked to an account.
    categorized is how many transactions the keywords categorized or sent back to triage.
    items_fetched is statement files, or receipts for a receipt provider."""

    run_id: int
    status: SyncStatus
    period: DateRange
    items_fetched: int
    message: str | None
    report: ImportReport | None
    linked: tuple[str, ...] = ()
    categorized: int = 0
    kind: ProviderKind = ProviderKind.bank

    @property
    def unlinked(self) -> tuple[str, ...]:
        """The last four digits of each statement account that no account is linked to, so its
        files could not be imported."""
        if self.report is None:
            return ()
        return tuple(sorted({f.unmapped_last4 for f in self.report.files if f.unmapped_last4}))


@dataclass(frozen=True)
class _Plan:
    """What a sync needs from the database, read before its SyncRun is written."""

    provider_id: int
    provider_key: str
    institution_key: str
    connector: Connector
    accounts: list[ConnectorAccount]
    vault_item_id: str | None


def bank_sync(
    session: Session,
    settings: Settings,
    *,
    provider_key: str,
    vault: Vault | None,
    prompt: Prompt,
    period: DateRange | None,
) -> SyncResult:
    """Fetch a bank provider's statement files for its accounts, then import the newly fetched
    ones. The caller holds the sync lock.

    The period defaults to 45 days before the latest transaction in those accounts, or to January 1
    of this year when there is none, through today. Raises ConnectorError if the provider is
    unknown, turned off, or has no open accounts. Connector failures do not raise: they set the
    result's status.
    """
    plan = _prepare(session, provider_key)
    window = period or _default_period(
        session, [account.account_id for account in plan.accounts], date.today()
    )
    run = start_run(session, plan.provider_id)
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
    vault: Vault | None,
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
        status, message = failure_of(exc)
        if not isinstance(exc, ConnectorError | VaultError):
            logger.exception("Fetch for provider %s failed unexpectedly", plan.provider_key)
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
        logger.exception("Import for provider %s stopped unexpectedly", plan.provider_key)
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
    """Categorize the provider's accounts as one batch, after identifying the counterparty as the
    automatic step does, because a rule can read it. Returns the transactions categorized,
    and a problem when the keywords could not be applied. The imported statements stay either
    way."""
    try:
        identifying = plan_counterparty(session)
        if identifying.preview is not None:
            apply_counterparty(session, identifying, idempotency_key=f"sync-counterparty:{uuid4()}")
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
        .where(Provider.key == provider_key, Provider.kind == ProviderKind.bank)
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
        provider_id=provider.id,
        provider_key=provider_key,
        institution_key=institution_key,
        connector=fetching_connector(provider.connector_key),
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
    items_fetched: int,
    message: str | None,
    window: DateRange,
    *,
    report: ImportReport | None = None,
    linked: tuple[str, ...] = (),
    categorized: int = 0,
) -> SyncResult:
    finish_run(session, run, status, items_fetched, message)
    return SyncResult(
        run_id=run.id,
        status=status,
        period=window,
        items_fetched=items_fetched,
        message=run.message,
        report=report,
        linked=linked,
        categorized=categorized,
    )


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
