"""The Data sync page: every provider, bank or receipts, as a card with its Sync, Edit and Delete,
and the sync status region. Adding a provider is the wizard of its kind (settings_wizard.py for a
bank, settings_vendor_wizard.py for receipts), and the vault is settings_vault.py."""

from html import escape
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import CHANGED_ELSEWHERE, BatchError
from omsync.core.vault import VaultError, VaultLocked, is_locked
from omsync.ingest.institutions import provider_group
from omsync.ingest.providers import ProviderSetupError
from omsync.ledger.accounts_view import update_account
from omsync.sync.providers import edit_provider, remove_provider
from omsync.sync.run import ALL, plan_sync
from omsync.web.accounts_panel import account_groups
from omsync.web.deps import get_db
from omsync.web.fragments import render_block
from omsync.web.routes.settings_vault import SYNC_PENDING, prompt_unlock
from omsync.web.routes.settings_wizard import check_files
from omsync.web.routes.shell import shell_context
from omsync.web.settings_views import SETTINGS_NAV, ProviderCardView, providers_summary
from omsync.web.sync_page import (
    all_cards,
    bank_groups,
    provider_card,
    provider_login_name,
    receipt_cards,
    status_view,
)
from omsync.web.sync_runner import SyncRefused
from omsync.web.vault_logins import VaultActionError, search_logins

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/settings_providers.html"
FRAGMENTS = "settings/provider_fragments.html"
PATH = "/settings/sync"
TABS = ("banks", "receipts")


@router.get(PATH, response_class=HTMLResponse)
def sync_page(request: Request, db: DbSession, tab: str = "banks") -> HTMLResponse:
    context = shell_context(request, db, active="", title="Data sync")
    groups = bank_groups(request, db)
    receipts = receipt_cards(request, db)
    context.update(
        nav=SETTINGS_NAV,
        active_href=PATH,
        page_title="Data sync",
        summary=providers_summary(all_cards(groups, receipts)),
        groups=groups,
        receipt_cards=receipts,
        sync=status_view(request, db),
        tab=tab if tab in TABS else "banks",
    )
    return request.app.state.templates.TemplateResponse(request, PAGE, context)


# Syncing from the page: a background job, its status region, and the question it may ask


@router.post(f"{PATH}/sync_all", response_class=HTMLResponse)
def sync_all_route(request: Request, db: DbSession) -> HTMLResponse:
    return _sync_request(request, db, ALL)


@router.get(f"{PATH}/status", response_class=HTMLResponse)
def sync_status_route(request: Request, db: DbSession, shown: int = 0) -> HTMLResponse:
    """The status region, as a poll sends it. shown is the question the page already displays, so
    the question form is sent again only when it has changed."""
    return _sync_reply(request, db, poll=True, shown=shown)


@router.post(f"{PATH}/answer", response_class=HTMLResponse)
def sync_answer_route(
    request: Request, db: DbSession, answer: Annotated[str, Form()] = ""
) -> HTMLResponse:
    """Give the answer to the question a sync is waiting on. The answer is never echoed back."""
    try:
        request.app.state.sync_runner.answer(answer)
    except SyncRefused as exc:
        return _sync_reply(request, db, status=422, notice=exc.message)
    return _sync_reply(request, db)


@router.post(f"{PATH}/cancel", response_class=HTMLResponse)
def sync_cancel_route(request: Request, db: DbSession) -> HTMLResponse:
    """Cancel the running sync, one provider's or Sync all. The region keeps polling until the
    provider in progress has stopped."""
    try:
        request.app.state.sync_runner.cancel()
    except SyncRefused as exc:
        return _sync_reply(request, db, status=422, notice=exc.message)
    return _sync_reply(request, db)


@router.post(f"{PATH}/{{provider_key}}/sync", response_class=HTMLResponse)
def sync_provider_route(request: Request, db: DbSession, provider_key: str) -> HTMLResponse:
    return _sync_request(request, db, provider_key)


# Editing a provider: its name, its vault login, and a bank's accounts


@router.get(f"{PATH}/{{provider_key}}/edit/login", response_class=HTMLResponse)
def edit_login_name(request: Request, db: DbSession, provider_key: str) -> HTMLResponse:
    """The name of the provider's vault login, which its edit dialog asks for when it opens and
    again once the vault is unlocked."""
    try:
        name = provider_login_name(request, db, provider_key)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such provider") from None
    return HTMLResponse(escape(name))


@router.get(f"{PATH}/{{provider_key}}/edit/logins", response_class=HTMLResponse)
def search_edit_logins(
    request: Request, db: DbSession, provider_key: str, q: str = ""
) -> HTMLResponse:
    """The login results for a search in one provider's edit form. A locked vault shows the unlock
    form in their place, and the search runs again once it is unlocked. Nothing is saved by a
    search."""
    _card(request, db, provider_key)
    vault = request.app.state.vault
    query = q.strip()
    logins = None
    message = ""
    if vault is not None and vault.is_unlocked() and query:
        try:
            logins = search_logins(vault, query, request.app.state.login_names)
        except VaultActionError as exc:
            message = exc.message
    return _fragment(
        request,
        "frag_provider_vault",
        key=provider_key,
        locked=is_locked(vault),
        logins=logins,
        message=message,
    )


@router.post(f"{PATH}/{{provider_key}}/edit", response_class=HTMLResponse)
def edit_route(
    request: Request,
    db: DbSession,
    provider_key: str,
    name: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
    vault_item_id: Annotated[str, Form()] = "",
    account: Annotated[list[str] | None, Form()] = None,
) -> HTMLResponse:
    """Save a provider's name and vault login, and for a bank the accounts it fetches, as one
    batch. Only the accounts checked in the form are kept, and a login is changed only when one
    was chosen from a search."""
    card = _card(request, db, provider_key)
    try:
        batch = edit_provider(
            db,
            request.app.state.settings,
            provider_key=provider_key,
            name=name.strip() or None,
            login_id=vault_item_id.strip() or None,
            account_ids=account or [],
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except ProviderSetupError as exc:
        return _failure(request, str(exc), 409 if exc.conflict else 422)
    if batch is None:
        return _fragment(request, "frag_provider_message", message="Nothing changed.", error=False)
    return _saved(request, db, batch, message=f"Saved {name.strip() or card.name}.")


# Linking statement accounts


@router.get(f"{PATH}/{{provider_key}}/link", response_class=HTMLResponse)
def link_accounts_route(request: Request, db: DbSession, provider_key: str) -> HTMLResponse:
    """The wizard's step for linking the statement accounts its files name, for one provider's
    institution. It is shown after a sync could not link an account by itself."""
    found = provider_group(db, provider_key)
    if found is None:
        raise HTTPException(status_code=404, detail="No such provider")
    group, provider = found
    return check_files(
        request,
        db,
        institution=group.key,
        name=group.name,
        connector=provider.connector_key,
        provider=provider.key,
    )


# Renaming an account from the list


@router.post(f"{PATH}/accounts/{{account_id}}/rename", response_class=HTMLResponse)
def rename_account_route(
    request: Request,
    db: DbSession,
    account_id: int,
    name: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Rename an account, as the account page's edit does: one undoable batch that changes only
    the name. A refused name (blank, too long, unchanged) answers 422 in the dialog, and an account
    that changed since the dialog opened answers 409."""
    try:
        batch = update_account(
            db, account_id, name=name, idempotency_key=idempotency_key or uuid4().hex
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="No such account") from None
    except ValueError as exc:
        return _failure(request, str(exc), 422)
    except BatchError:
        return _failure(request, CHANGED_ELSEWHERE, 409)
    return _saved(request, db, batch, message=str((batch.summary or {}).get("label", "Renamed.")))


# Deleting a provider


@router.post(f"{PATH}/{{provider_key}}/delete", response_class=HTMLResponse)
def delete_route(
    request: Request,
    db: DbSession,
    provider_key: str,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Delete a provider as one batch. A bank's accounts and statements stay, and so do a receipt
    provider's receipts."""
    card = _card(request, db, provider_key)
    settings = request.app.state.settings
    key = idempotency_key or uuid4().hex
    try:
        batch = remove_provider(db, settings, provider_key=provider_key, idempotency_key=key)
    except ProviderSetupError as exc:
        return _failure(request, str(exc), 409 if exc.conflict else 422)
    return _saved(request, db, batch, message=f"Deleted {card.name}.")


def provider_undo_reply(request: Request, db: Session) -> HTMLResponse:
    """After an undo of a provider batch: the toast, and the lists re-read out of band."""
    return _fragment(
        request,
        "frag_provider_undone",
        message="Undone",
        groups=bank_groups(request, db),
        receipt_cards=receipt_cards(request, db),
        panel=account_groups(db),
    )


# Helpers


def _saved(request: Request, db: Session, batch: Batch, *, message: str) -> HTMLResponse:
    return _fragment(
        request,
        "frag_provider_saved",
        groups=bank_groups(request, db),
        receipt_cards=receipt_cards(request, db),
        panel=account_groups(db),
        message=message,
        batch_id=batch.id,
    )


def _failure(request: Request, message: str, status: int) -> HTMLResponse:
    return _fragment(request, "frag_provider_message", status=status, message=message, error=True)


def _sync_request(request: Request, db: Session, scope: str) -> HTMLResponse:
    """Start a sync, or show why it cannot start. A locked vault opens the vault panel instead,
    and the sync starts once it is unlocked."""
    try:
        plan = plan_sync(db, scope, has_vault=request.app.state.vault is not None)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such provider") from None
    if plan.refusal is not None:
        return _sync_reply(request, db, status=422, notice=plan.refusal)
    if plan.needs_vault and _vault_needs_unlock(request):
        return prompt_unlock(request, _sync_reply(request, db), SYNC_PENDING + scope)
    try:
        request.app.state.sync_runner.start(scope, plan.bank_keys, plan.receipt_keys)
    except SyncRefused as exc:
        return _sync_reply(request, db, status=422, notice=exc.message)
    return _sync_reply(request, db)


def _vault_needs_unlock(request: Request) -> bool:
    """Whether the vault must be unlocked before a sync. This asks the vault itself, so a vault
    that was locked from outside opens the panel and not a failed sync."""
    state = request.app.state
    if is_locked(state.vault):
        return True
    try:
        state.vault.verify()
    except VaultLocked:
        state.login_names.clear()
        return True
    except VaultError:
        return False
    return False


def _sync_reply(
    request: Request,
    db: Session,
    *,
    status: int = 200,
    notice: str = "",
    poll: bool = False,
    shown: int = 0,
) -> HTMLResponse:
    """The status region. While a job runs, a poll sends only the region and, when the question or
    notice changed, the question slot, so the buttons and the lists are not redrawn each time.
    Otherwise the buttons and the provider lists go out of band too, so they show the job's
    state."""
    sync = status_view(request, db)
    controls = not (poll and sync.running)
    return _fragment(
        request,
        "frag_sync",
        status=status,
        sync=sync,
        notice=notice,
        include_question=not poll or shown != sync.shown_number,
        groups=bank_groups(request, db) if controls else None,
        receipt_cards=receipt_cards(request, db) if controls else None,
    )


def _card(request: Request, db: Session, provider_key: str) -> ProviderCardView:
    try:
        return provider_card(request, db, provider_key)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such provider") from None


def _fragment(
    request: Request,
    block: str,
    *,
    status: int = 200,
    **context: object,
) -> HTMLResponse:
    return render_block(request, FRAGMENTS, block, status=status, **context)
