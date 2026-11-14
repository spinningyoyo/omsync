"""View data for the Settings pages: the sub-nav, and the rows and labels the templates draw.

Nothing here reads the database or the vault. Account numbers appear only as ••last4, and file
names and messages pass through mask_account_numbers before they are shown.
"""

from dataclasses import dataclass, field
from datetime import date
from uuid import uuid4

from omsync.ingest.discovery import DiscoveredAccount, mask_account_numbers
from omsync.ingest.institutions import AccountLine, InstitutionGroup, ProviderLine
from omsync.ingest.pipeline import ImportReport
from omsync.ingest.providers import connector_label
from omsync.ledger.models import AccountType
from omsync.ledger.sources import account_digits
from omsync.web.account_page import SyncView, sync_views
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
            NavItem("Bank sync", "/settings/providers", "banknote-arrow-down"),
            NavItem("Contacts", "/settings/contacts", "book-user"),
            NavItem("Rental properties", "/settings/rental", "house"),
            NavItem("About", "/settings/about", "info"),
        ),
    ),
    NavSection(
        "Transactions",
        (NavItem("Categories", "/settings/categories", "shapes"),),
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
    key: str
    connector: str  # the card's Method line, without the bank name its header already shows
    bank: str  # the connector's full name, for the edit dialog
    vault: str
    locked: bool  # the vault is locked, so the edit form offers its unlock form first
    sync_busy: bool  # a sync job is running, so this provider's Sync button is disabled
    sync: SyncView | None
    accounts: list[AccountRowView]
    choices: list[AccountChoiceView]
    edit_key: str
    delete_key: str


@dataclass(frozen=True)
class InstitutionGroupView:
    key: str
    name: str
    providers: list[ProviderCardView]
    not_synced: list[AccountRowView]


def institution_group_view(
    group: InstitutionGroup, vault_texts: dict[str, str], locked: bool, busy: bool
) -> InstitutionGroupView:
    """One institution's providers, and its open accounts that no provider fetches."""
    return InstitutionGroupView(
        key=group.key,
        name=group.name,
        providers=[
            provider_card_view(group, provider, vault_texts.get(provider.key, ""), locked, busy)
            for provider in group.providers
        ],
        not_synced=[
            account_row(account)
            for account in group.accounts
            if not account.closed and group.provider_of(account.id) is None
        ],
    )


def provider_card_view(
    group: InstitutionGroup, provider: ProviderLine, vault_text: str, locked: bool, busy: bool
) -> ProviderCardView:
    fetched = set(provider.account_ids)
    return ProviderCardView(
        key=provider.key,
        connector=_method_text(group, provider),
        bank=_bank_text(provider),
        vault=vault_text,
        locked=locked,
        sync_busy=busy,
        sync=_sync_view(provider),
        accounts=[account_row(account) for account in group.accounts if account.id in fetched],
        choices=[
            _choice_view(provider, account)
            for account in group.accounts
            if account.id in fetched
            or (not account.closed and group.provider_of(account.id) is None)
        ],
        edit_key=uuid4().hex,
        delete_key=uuid4().hex,
    )


def _choice_view(provider: ProviderLine, account: AccountLine) -> AccountChoiceView:
    return AccountChoiceView(
        id=account.id,
        label=(
            f"{account.name} {last4_text(account.last4, account.alias_last4)}"
            f" · {type_text(account.type)}"
        ),
        checked=account.id in provider.account_ids,
    )


def _sync_view(provider: ProviderLine) -> SyncView | None:
    syncs = sync_views([provider.last_sync]) if provider.last_sync is not None else []
    return syncs[0] if syncs else None


def _bank_text(provider: ProviderLine) -> str:
    return connector_label(provider.connector_key) + (
        "" if provider.connector_enabled else " (turned off)"
    )


def _method_text(group: InstitutionGroup, provider: ProviderLine) -> str:
    """How the provider fetches, for the card's Method line. The card's header already names the
    institution, so a label's bank name is dropped when it is that name. A name that goes further,
    such as Chase Business under Chase, is kept: it says which login the provider uses."""
    label = connector_label(provider.connector_key)
    bank, separator, method = label.partition(": ")
    name = group.name.casefold()
    if separator and (bank.casefold() == name or not bank.casefold().startswith(name)):
        label = method
    return label + ("" if provider.connector_enabled else " (turned off)")


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
