"""View data for the Settings pages: the sub-nav, and the rows and labels the templates draw.

Nothing here reads the database or the vault. Account numbers appear only as ••last4, and file
names and messages pass through mask_account_numbers before they are shown.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from uuid import uuid4

from omsync.ingest.discovery import DiscoveredAccount, mask_account_numbers
from omsync.ingest.institutions import AccountLine, InstitutionGroup, ProviderLine
from omsync.ingest.pipeline import ImportReport
from omsync.ledger.models import AccountType, ProviderKind
from omsync.ledger.sources import account_digits
from omsync.ledger.sync_runs import RecentSync
from omsync.receipts.connectors import get_connector
from omsync.receipts.profiles import VendorProfile
from omsync.web.account_page import SyncView, local_time, sync_views
from omsync.web.accounts_panel import TYPE_LABELS
from omsync.web.formats import long_day


@dataclass(frozen=True)
class WizardState:
    """The values the wizard carries from step to step in hidden fields. Each step checks them."""

    institution: str = ""
    name: str = ""
    connector: str = ""
    provider: str = ""


@dataclass(frozen=True)
class NavItem:
    label: str
    href: str | None  # None marks a slot whose page is not built yet
    icon: str | None = None


@dataclass(frozen=True)
class NavSection:
    title: str
    items: tuple[NavItem, ...]


SETTINGS_NAV: tuple[NavSection, ...] = (
    NavSection(
        "Learn",
        (NavItem("Guides", "/settings/guides", "book-open"),),
    ),
    NavSection(
        "General",
        (
            NavItem("Data sync", "/settings/sync", "cloud-sync"),
            NavItem("Contacts", "/settings/contacts", "book-user"),
            NavItem("Rental properties", "/settings/rental", "house"),
            NavItem("About", "/settings/about", "info"),
        ),
    ),
    NavSection(
        "Transactions",
        (
            NavItem("Categories", "/settings/categories", "shapes"),
            NavItem("Tags", "/settings/tags", "tag"),
        ),
    ),
)


def last4_text(last4: str | None, alias_last4: str | None = None) -> str:
    """The masked digits for display (see account_digits). A missing or masked value shows as a
    dash."""
    if not last4 or set(last4) == {"*"}:
        return "—"
    return account_digits(last4, alias_last4)


def span_text(start: date | None, end: date | None) -> str:
    days = [day for day in (start, end) if day is not None]
    if not days:
        return "no dates in the files"
    if len(days) == 1 or days[0] == days[-1]:
        return long_day(days[0])
    return f"{long_day(days[0])} to {long_day(days[-1])}"


def type_text(account_type: AccountType) -> str:
    return TYPE_LABELS[account_type]


def type_choices() -> list[tuple[str, str]]:
    """(value, label) for every account type, for the type selects."""
    return [(account_type.value, TYPE_LABELS[account_type]) for account_type in AccountType]


def type_hint_value(hint: str | None) -> str:
    """The account type to preselect: the file's hint when it has one, otherwise Other."""
    try:
        return AccountType(hint).value if hint else AccountType.other.value
    except ValueError:
        return AccountType.other.value


@dataclass(frozen=True)
class TagView:
    id: int
    name: str
    color: str
    count: int


@dataclass(frozen=True)
class AccountRowView:
    id: int
    name: str
    last4: str
    type: str
    joint: bool
    closed: bool
    rename_key: str = field(default_factory=lambda: uuid4().hex)  # one rename applies once


@dataclass(frozen=True)
class AccountChoiceView:
    """One account on a provider's edit form."""

    id: int
    label: str
    checked: bool


@dataclass(frozen=True)
class ProviderCardView:
    """One provider, of either kind, as its card draws it. A bank provider has accounts and
    choices; a receipt provider has receipt_count and latest_date."""

    key: str
    name: str
    kind: ProviderKind
    logo_key: str
    login: str  # whether a vault login is picked, or how you sign in
    locked: bool  # the vault is locked, so a login search cannot run
    sync_busy: bool  # a sync job is running, so this provider's Sync button is disabled
    sync: SyncView | None
    edit_key: str
    delete_key: str
    synced_at: datetime | None = None  # when its last sync started, for the page's summary
    accounts: list[AccountRowView] = field(default_factory=list)
    choices: list[AccountChoiceView] = field(default_factory=list)
    receipt_count: int = 0
    latest_date: date | None = None
    uses_vault: bool = True  # the connector can sign in with a vault login
    login_picked: bool = False  # a vault login is picked, so its edit dialog can name it

    @property
    def open_accounts(self) -> list[AccountRowView]:
        return [account for account in self.accounts if not account.closed]

    @property
    def closed_accounts(self) -> list[AccountRowView]:
        return [account for account in self.accounts if account.closed]


@dataclass(frozen=True)
class InstitutionGroupView:
    key: str
    name: str
    providers: list[ProviderCardView]
    not_synced: list[AccountRowView]


def receipt_card_view(
    profile: VendorProfile,
    login: str,
    receipt_count: int,
    latest_date: date | None,
    *,
    locked: bool,
    busy: bool,
    run: RecentSync | None,
) -> ProviderCardView:
    return ProviderCardView(
        key=profile.key,
        name=profile.name,
        kind=ProviderKind.receipt,
        logo_key=profile.vendor_key,
        login=login,
        locked=locked,
        sync_busy=busy,
        sync=_run_view(run),
        edit_key=uuid4().hex,
        delete_key=uuid4().hex,
        synced_at=run.started_at if run is not None else None,
        receipt_count=receipt_count,
        latest_date=latest_date,
        uses_vault=get_connector(profile.vendor_key).uses_vault,
        login_picked=profile.vault_item_id is not None,
    )


def institution_group_view(
    group: InstitutionGroup, logins: dict[str, str], locked: bool, busy: bool
) -> InstitutionGroupView:
    """One institution's providers, and its open accounts that no provider fetches."""
    return InstitutionGroupView(
        key=group.key,
        name=group.name,
        providers=[
            provider_card_view(group, provider, logins.get(provider.key, ""), locked, busy)
            for provider in group.providers
        ],
        not_synced=[
            account_row(account)
            for account in group.accounts
            if not account.closed and group.provider_of(account.id) is None
        ],
    )


def provider_card_view(
    group: InstitutionGroup, provider: ProviderLine, login: str, locked: bool, busy: bool
) -> ProviderCardView:
    fetched = set(provider.account_ids)
    return ProviderCardView(
        key=provider.key,
        name=provider.name,
        kind=ProviderKind.bank,
        logo_key=group.key,
        login=login,
        locked=locked,
        sync_busy=busy,
        sync=_run_view(provider.last_sync),
        edit_key=uuid4().hex,
        delete_key=uuid4().hex,
        synced_at=provider.last_sync.started_at if provider.last_sync is not None else None,
        accounts=[account_row(account) for account in group.accounts if account.id in fetched],
        choices=[
            _choice_view(provider, account)
            for account in group.accounts
            if account.id in fetched
            or (not account.closed and group.provider_of(account.id) is None)
        ],
        login_picked=provider.vault_item_id is not None,
    )


def providers_summary(cards: list[ProviderCardView]) -> str:
    """The page's line under its title: how many providers, and when the latest sync ran."""
    if not cards:
        return "Where omsync signs in to fetch your statements and receipts."
    count = f"{len(cards)} provider" + ("" if len(cards) == 1 else "s")
    synced = [card.synced_at for card in cards if card.synced_at is not None]
    when = f"last sync {local_time(max(synced))}" if synced else "no sync yet"
    return f"{count} · {when}"


def _choice_view(provider: ProviderLine, account: AccountLine) -> AccountChoiceView:
    return AccountChoiceView(
        id=account.id,
        label=(
            f"{account.name} {last4_text(account.last4, account.alias_last4)}"
            f" · {type_text(account.type)}"
        ),
        checked=account.id in provider.account_ids,
    )


def _run_view(run: RecentSync | None) -> SyncView | None:
    return sync_views([run])[0] if run is not None else None


def account_row(account: AccountLine) -> AccountRowView:
    return AccountRowView(
        id=account.id,
        name=account.name,
        last4=last4_text(account.last4, account.alias_last4),
        type=type_text(account.type),
        joint=account.joint,
        closed=account.closed,
    )


@dataclass(frozen=True)
class MappingRowView:
    token: str
    last4: str
    type_hint: str
    type_default: str
    file_count: int
    span: str
    default_name: str
    preselect: int | None  # the account to preselect as "Same as existing account", if any
    choice_default: str  # "alias" when there is a preselected account, otherwise "new"


def mapping_row(
    account: DiscoveredAccount, token: str, preselect: int | None = None
) -> MappingRowView:
    account_type = type_hint_value(account.account_type_hint)
    return MappingRowView(
        token=token,
        last4=last4_text(account.last4),
        type_hint=TYPE_LABELS[AccountType(account_type)],
        type_default=account_type,
        file_count=account.file_count,
        span=span_text(account.period_start, account.period_end),
        default_name=TYPE_LABELS[AccountType(account_type)],
        preselect=preselect,
        choice_default="new" if preselect is None else "alias",
    )


@dataclass(frozen=True)
class FileRowView:
    name: str
    account: str
    joined: int
    created: int
    review: int
    already_imported: bool
    error: str | None


FILE_COLUMNS: tuple[dict[str, object], ...] = (
    {"header": "File", "numeric": False},
    {"header": "Account", "numeric": False},
    {"header": "Joined", "numeric": True},
    {"header": "Created", "numeric": True},
    {"header": "Review", "numeric": True},
    {"header": "Status", "numeric": False},
)


@dataclass(frozen=True)
class ImportView:
    applied: bool
    files: list[FileRowView]
    planned: int
    failures: int
    review: int
    auto_linked: int = 0
    auto_problem: str | None = None

    @property
    def columns(self) -> tuple[dict[str, object], ...]:
        return FILE_COLUMNS

    @property
    def table_rows(self) -> list[list[str]]:
        return [
            [
                file.name,
                file.account or "none",
                str(file.joined),
                str(file.created),
                str(file.review),
                _file_status(file),
            ]
            for file in self.files
        ]


def _file_status(file: FileRowView) -> str:
    if file.error:
        return file.error
    if file.already_imported:
        return "Already imported"
    return "Imported" if file.created or file.joined or file.review else "Nothing to import"


def import_view(report: ImportReport) -> ImportView:
    files = [
        FileRowView(
            name=mask_account_numbers(result.name),
            account=(
                last4_text(result.account_last4, result.account_alias_last4)
                if result.account_last4
                else ""
            ),
            joined=result.joined,
            created=result.created,
            review=result.review,
            already_imported=result.already_imported,
            error=None if result.error is None else mask_account_numbers(result.error),
        )
        for result in report.files
    ]
    return ImportView(
        applied=report.applied,
        files=files,
        planned=report.planned,
        failures=report.failures,
        review=report.review,
        auto_linked=report.auto.linked,
        auto_problem=None
        if report.auto.problem is None
        else mask_account_numbers(report.auto.problem),
    )


@dataclass(frozen=True)
class Outcome:
    """One mapping the wizard tried to save, for the step's list of results."""

    ok: bool
    text: str


@dataclass(frozen=True)
class MappedRowView:
    last4: str
    account: str


@dataclass(frozen=True)
class AccountFormRow:
    """One of the four rows of the bank step's "Accounts to sync" form."""

    index: int
    last4: str = ""
    name: str = ""
    type: str = AccountType.checking.value
    joint: bool = False


def blank_account_rows(count: int) -> list[AccountFormRow]:
    return [AccountFormRow(index=index) for index in range(count)]
