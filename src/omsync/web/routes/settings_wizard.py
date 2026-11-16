"""Add-provider wizard responses and account mapping."""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from omsync.core.vault import is_locked
from omsync.ingest.discovery import scan_statement_files, suggested_account
from omsync.ingest.institutions import institution_accounts
from omsync.ingest.pipeline import ImportReport, import_institution
from omsync.ingest.providers import (
    ProviderSetupError,
    add_provider,
    connector_choices,
    create_wizard_accounts,
    institution_exists,
    last4_is_statement_id,
    map_existing_account,
    map_new_account,
    provider_setup,
)
from omsync.ledger.accounts import NewAccount, last4_of
from omsync.web.deps import get_db
from omsync.web.fragments import render_block
from omsync.web.routes.shell import shell_context
from omsync.web.settings_views import (
    SETTINGS_NAV,
    AccountFormRow,
    MappedRowView,
    Outcome,
    WizardState,
    account_row,
    blank_account_rows,
    import_view,
    last4_text,
    mapping_row,
    type_choices,
)
from omsync.web.vault_logins import (
    VaultActionError,
    search_logins,
    unlock_vault,
    vault_status_view,
)

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/settings_wizard.html"
FRAGMENTS = "settings/fragments.html"
PROVIDERS_PATH = "/settings/providers"
WIZARD = "/settings/providers/new"
MAX_FORM_VALUE = 200
ACCOUNT_ROWS = 4
STALE_IMPORT = (
    "The files or the ledger changed after this preview. Review it again before importing."
)


@router.get(WIZARD, response_class=HTMLResponse)
def wizard_page(request: Request, db: DbSession) -> HTMLResponse:
    context = shell_context(request, db, active="", title="Add provider")
    context.update(
        nav=SETTINGS_NAV,
        active_href=PROVIDERS_PATH,
        page_title="Add provider",
        state=WizardState(),
        connectors=connector_choices(),
        form_key=uuid4().hex,
        message="",
    )
    return request.app.state.templates.TemplateResponse(request, PAGE, context)


# Step 1: institution, provider, and bank


@router.post(f"{WIZARD}/connector", response_class=HTMLResponse)
def choose_connector(
    request: Request,
    db: DbSession,
    institution: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    provider: Annotated[str, Form()] = "",
    connector: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Check the institution, the provider's key, and the bank, then go on to the vault login."""
    key_value = idempotency_key or uuid4().hex
    try:
        setup = provider_setup(
            db,
            institution=institution[:MAX_FORM_VALUE],
            name=name[:MAX_FORM_VALUE],
            provider=provider[:MAX_FORM_VALUE],
            connector=connector[:MAX_FORM_VALUE],
        )
    except ProviderSetupError as exc:
        state = WizardState(
            institution.strip().lower(), name.strip(), connector, provider.strip().lower()
        )
        return _step(
            request,
            "frag_step_setup",
            422,
            **_setup_context(state, key_value, str(exc)),
        )
    state = WizardState(
        setup.institution_key,
        setup.institution_name,
        setup.connector_key,
        setup.provider_key,
    )
    vault = request.app.state.vault
    return _step(
        request,
        "frag_step_login",
        **_login_context(state, locked=is_locked(vault), form_key=uuid4().hex),
    )


# Step 2: vault login


@router.get(f"{WIZARD}/logins", response_class=HTMLResponse)
def search_vault(
    request: Request,
    institution: str = "",
    name: str = "",
    connector: str = "",
    provider: str = "",
    q: str = "",
) -> HTMLResponse:
    """The login results for a search, debounced in the browser. A locked vault shows the unlock
    form in their place. Nothing is saved by a search."""
    vault = request.app.state.vault
    state = _state(institution, name, connector, provider)
    query = q.strip()
    if is_locked(vault):
        return _fragment(
            request, "frag_vault_results", state=state, locked=True, logins=None, query=query
        )
    if vault is None or not query:
        return _fragment(
            request, "frag_vault_results", state=state, locked=False, logins=None, query=query
        )
    try:
        logins = search_logins(vault, query, request.app.state.login_names)
    except VaultActionError as exc:
        return _fragment(
            request,
            "frag_vault_results",
            state=state,
            locked=False,
            logins=None,
            query=query,
            message=exc.message,
        )
    return _fragment(
        request, "frag_vault_results", state=state, locked=False, logins=logins, query=query
    )


@router.post(f"{WIZARD}/unlock", response_class=HTMLResponse)
def unlock(
    request: Request,
    institution: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    connector: Annotated[str, Form()] = "",
    provider: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Unlock the vault with the master password. The password is not echoed, stored, or logged,
    and a failure shows the vault's own message."""
    vault = request.app.state.vault
    state = _state(institution, name, connector, provider)
    try:
        unlock_vault(vault, request.app.state.login_limiter, password)
    except VaultActionError as exc:
        return _step(
            request,
            "frag_step_login",
            422,
            **_login_context(
                state,
                locked=is_locked(vault),
                form_key=uuid4().hex,
                unlock_message=exc.message,
            ),
        )
    return _step(
        request,
        "frag_step_login",
        **_login_context(state, locked=False, form_key=uuid4().hex),
        vault_status=vault_status_view(vault),
    )


@router.post(f"{WIZARD}/login", response_class=HTMLResponse)
def save_login(
    request: Request,
    db: DbSession,
    institution: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    connector: Annotated[str, Form()] = "",
    provider: Annotated[str, Form()] = "",
    vault_item_id: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Save the provider with its bank and vault login, as one batch. Its institution is created
    first when it is new."""
    state = _state(institution, name, connector, provider)
    try:
        add_provider(
            db,
            institution=institution,
            name=name,
            provider=provider,
            connector=connector,
            vault_login=vault_item_id or None,
            has_vault=request.app.state.vault is not None,
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except ProviderSetupError as exc:
        vault = request.app.state.vault
        return _step(
            request,
            "frag_step_login",
            422,
            **_login_context(
                state,
                locked=is_locked(vault),
                form_key=uuid4().hex,
                message=str(exc),
            ),
        )
    return _step(
        request,
        "frag_step_accounts",
        **_accounts_context(request, db, state, rows=blank_account_rows(ACCOUNT_ROWS)),
    )


# Step 3: the accounts to sync


@router.post(f"{WIZARD}/accounts", response_class=HTMLResponse)
async def save_accounts(request: Request, db: DbSession) -> HTMLResponse:
    """Create the accounts the bank will sync, as one batch. Each row names an account by its last
    four digits, a name, a type, and joint. No statement account ID is invented; for BoA the last
    four digits are the ID, so an alias is added there. Blank rows are skipped."""
    form = await request.form()
    state = _state_from_form(form)
    rows = _form_account_rows(form)
    used = [row for row in rows if row.last4.strip() or row.name.strip()]
    try:
        if not used:
            if not any(
                not account.closed for account in institution_accounts(db, state.institution)
            ):
                raise ProviderSetupError("Add at least one account, with its last four digits.")
            return _step(
                request,
                "frag_step_accounts",
                **_accounts_context(
                    request, db, state, rows=rows, message="No new accounts to save."
                ),
            )
        create_wizard_accounts(
            db,
            institution=state.institution,
            connector=state.connector,
            provider_key=state.provider,
            accounts=[
                NewAccount(last4=row.last4, name=row.name, type=row.type, joint=row.joint)
                for row in used
            ],
            idempotency_key=str(form.get("idempotency_key") or uuid4().hex)[:MAX_FORM_VALUE],
        )
    except ProviderSetupError as exc:
        return _step(
            request,
            "frag_step_accounts",
            422,
            **_accounts_context(request, db, state, rows=rows, message=str(exc), error=True),
        )
    saved = len(used)
    return _step(
        request,
        "frag_step_accounts",
        **_accounts_context(
            request,
            db,
            state,
            rows=blank_account_rows(ACCOUNT_ROWS),
            message=f"Saved {saved} account{'s' if saved != 1 else ''}.",
        ),
    )


# Step 4: check for statement files


@router.post(f"{WIZARD}/check", response_class=HTMLResponse)
def check_files(
    request: Request,
    db: DbSession,
    institution: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    connector: Annotated[str, Form()] = "",
    provider: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Look in the institution's folder for statement files and list the accounts they name."""
    state = _state(institution, name, connector, provider)
    if not institution_exists(db, state.institution):
        return _step(
            request,
            "frag_step_setup",
            422,
            **_setup_context(
                state,
                uuid4().hex,
                "Save the provider first. Start the wizard again.",
            ),
        )
    return _step(request, "frag_step_map", **_map_context(request, db, state, outcomes=[]))


# Step 4: map accounts


@router.post(f"{WIZARD}/map", response_class=HTMLResponse)
async def save_mappings(request: Request, db: DbSession) -> HTMLResponse:
    """Save one batch per mapping the user chose. A row is named by its token, so the statement
    account ID is looked up here and is never posted back. Each mapping has its own idempotency
    key, so a double submit applies each once."""
    form = await request.form()
    state = _state_from_form(form)
    form_key = str(form.get("idempotency_key") or uuid4().hex)[:MAX_FORM_VALUE]
    tokens = request.app.state.mapping_tokens
    outcomes: list[Outcome] = []
    for raw_token in form.getlist("row"):
        token = str(raw_token)[:MAX_FORM_VALUE]
        choice = str(form.get(f"choice_{token}", "skip"))
        if choice == "skip":
            continue
        statement_account_id = tokens.resolve(token, state.institution)
        if statement_account_id is None:
            outcomes.append(Outcome(False, "This row expired. Check for files again."))
            continue
        label = last4_text(last4_of(statement_account_id))
        key = f"settings-map:{form_key}:{token}"
        try:
            if choice == "new":
                name = str(form.get(f"name_{token}", ""))
                map_new_account(
                    db,
                    institution=state.institution,
                    statement_account_id=statement_account_id,
                    name=name,
                    account_type=str(form.get(f"type_{token}", "")),
                    joint=form.get(f"joint_{token}") == "1",
                    idempotency_key=key,
                )
                outcomes.append(Outcome(True, f"{label} is now account {name.strip()}."))
            elif choice == "alias":
                account_id = _account_id(form.get(f"account_{token}", ""))
                map_existing_account(
                    db,
                    institution=state.institution,
                    statement_account_id=statement_account_id,
                    account_id=account_id,
                    idempotency_key=key,
                )
                outcomes.append(Outcome(True, f"{label} now belongs to an existing account."))
            else:
                outcomes.append(Outcome(False, "Choose one of the options for each account."))
        except ProviderSetupError as exc:
            outcomes.append(Outcome(False, f"{label}: {exc}"))
    return _step(
        request,
        "frag_step_map",
        **_map_context(request, db, state, outcomes=outcomes),
    )


# Step 5: preview and import


@router.post(f"{WIZARD}/preview", response_class=HTMLResponse)
def preview(
    request: Request,
    db: DbSession,
    institution: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    connector: Annotated[str, Form()] = "",
    provider: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """What importing the institution's pending files would do. Writes nothing."""
    state = _state(institution, name, connector, provider)
    shown = import_institution(db, request.app.state.settings, state.institution, apply=False)
    return _preview_step(request, db, state, shown)


@router.post(f"{WIZARD}/import", response_class=HTMLResponse)
def import_files_step(
    request: Request,
    db: DbSession,
    institution: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    connector: Annotated[str, Form()] = "",
    provider: Annotated[str, Form()] = "",
    identity: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Import the pending files, each as its own batch. import_files gives each apply a fresh key,
    and a file already imported is skipped, so a repeated click imports nothing twice.

    `identity` is the preview the person saw. The import runs only if the files and the ledger
    still produce that same preview; otherwise the preview is shown again, with a message."""
    state = _state(institution, name, connector, provider)
    current = import_institution(db, request.app.state.settings, state.institution, apply=False)
    if current.identity != identity:
        return _preview_step(request, db, state, current, status_code=409, notice=STALE_IMPORT)
    report = import_institution(db, request.app.state.settings, state.institution, apply=True)
    return _step(
        request,
        "frag_step_imported",
        state=state,
        view=import_view(report),
        accounts=[
            {
                "id": account.id,
                "label": f"{account.name} {last4_text(account.last4, account.alias_last4)}",
            }
            for account in institution_accounts(db, state.institution)
            if not account.closed
        ],
    )


# Contexts and helpers


def _preview_step(
    request: Request,
    db: Session,
    state: WizardState,
    report: ImportReport,
    *,
    status_code: int = 200,
    notice: str = "",
) -> HTMLResponse:
    """The preview step. Its Import button carries the report's identity, so the import runs only
    if the files and the ledger still match what this step shows."""
    view = import_view(report)
    return _step(
        request,
        "frag_step_preview",
        status_code=status_code,
        state=state,
        view=view,
        can_import=view.planned > 0,
        identity=report.identity,
        notice=notice,
    )


def _state(institution: str, name: str, connector: str, provider: str) -> WizardState:
    """The hidden values as the form sent them, cut to the field limit. Each step checks them."""
    return WizardState(
        institution[:MAX_FORM_VALUE],
        name[:MAX_FORM_VALUE],
        connector[:MAX_FORM_VALUE],
        provider[:MAX_FORM_VALUE],
    )


def _state_from_form(form: FormData) -> WizardState:
    return _state(
        str(form.get("institution", "")),
        str(form.get("name", "")),
        str(form.get("connector", "")),
        str(form.get("provider", "")),
    )


def _setup_context(state: WizardState, form_key: str, message: str) -> dict[str, object]:
    return {
        "state": state,
        "connectors": connector_choices(),
        "form_key": form_key,
        "message": message,
    }


def _login_context(
    state: WizardState,
    *,
    locked: bool,
    form_key: str,
    message: str = "",
    unlock_message: str = "",
) -> dict[str, object]:
    return {
        "state": state,
        "locked": locked,
        "logins": None,
        "query": "",
        "form_key": form_key,
        "message": message,
        "unlock_message": unlock_message,
    }


def _accounts_context(
    request: Request,
    db: Session,
    state: WizardState,
    *,
    rows: list[AccountFormRow],
    message: str = "",
    error: bool = False,
) -> dict[str, object]:
    return {
        "state": state,
        "existing": [
            account_row(account) for account in institution_accounts(db, state.institution)
        ],
        "rows": rows,
        "types": type_choices(),
        "last4_is_statement_id": last4_is_statement_id(state.connector),
        "form_key": uuid4().hex,
        "message": message,
        "error": error,
    }


def _form_account_rows(form: FormData) -> list[AccountFormRow]:
    """The four rows of the accounts form, as typed. They are checked by the domain, not here."""
    get = form.get
    return [
        AccountFormRow(
            index=index,
            last4=str(get(f"last4_{index}", ""))[:MAX_FORM_VALUE],
            name=str(get(f"name_{index}", ""))[:MAX_FORM_VALUE],
            type=str(get(f"type_{index}", "checking"))[:MAX_FORM_VALUE],
            joint=get(f"joint_{index}") == "1",
        )
        for index in range(ACCOUNT_ROWS)
    ]


def _map_context(
    request: Request, db: Session, state: WizardState, *, outcomes: list[Outcome]
) -> dict[str, object]:
    """The mapping step's rows. Each unmapped statement account gets a fresh token, and the tokens
    for this institution from any earlier listing are forgotten first."""
    settings = request.app.state.settings
    tokens = request.app.state.mapping_tokens
    scan = scan_statement_files(db, settings, state.institution)
    tokens.revoke_institution(state.institution)
    accounts = institution_accounts(db, state.institution)
    names = {account.id: account.name for account in accounts}
    rows = []
    for account in scan.accounts:
        if account.mapped_account_id is not None:
            continue
        match = suggested_account(db, account)
        rows.append(
            mapping_row(
                account,
                tokens.issue(state.institution, account.statement_account_id),
                preselect=None if match is None else match.id,
            )
        )
    mapped = [
        MappedRowView(
            last4=last4_text(account.last4), account=names.get(account.mapped_account_id or 0, "")
        )
        for account in scan.accounts
        if account.mapped_account_id is not None
    ]
    return {
        "state": state,
        "rows": rows,
        "mapped": mapped,
        "unreadable": list(scan.unreadable),
        "types": type_choices(),
        "picker": [
            (account.id, f"{account.name} {last4_text(account.last4, account.alias_last4)}")
            for account in accounts
            if not account.closed
        ],
        "form_key": uuid4().hex,
        "outcomes": outcomes,
    }


def _account_id(value: object) -> int:
    try:
        return int(str(value))
    except ValueError:
        raise ProviderSetupError("Choose an existing account from the list.") from None


def _step(request: Request, block: str, status_code: int = 200, **context: object) -> HTMLResponse:
    return _fragment(request, block, status_code=status_code, **context)


def _fragment(
    request: Request, block: str, *, status_code: int = 200, **context: object
) -> HTMLResponse:
    return render_block(request, FRAGMENTS, block, status=status_code, **context)
