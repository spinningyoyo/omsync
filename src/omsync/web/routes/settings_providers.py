"""Bank sync: each institution's providers and the accounts they fetch, and editing and deleting a
provider as batches. Adding one is the wizard in settings_wizard.py."""

from dataclasses import replace
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import BatchError
from omsync.core.sync_models import SyncKind, latest_sync_runs
from omsync.core.vault import is_locked
from omsync.ingest.connectors.config import enabled_provider_keys
from omsync.ingest.institutions import bank_sync_groups, provider_group, sync_refusal
from omsync.ingest.providers import ProviderSetupError, edit_provider, remove_provider
from omsync.ledger.accounts_view import update_account
from omsync.receipts.profiles import (
    delete_vendor_profile,
    get_vendor_profile,
    latest_receipt_date,
    list_vendor_profiles,
    save_vendor_profile,
    vendor_receipt_count,
)
from omsync.web.account_page import sync_views
from omsync.web.accounts_panel import account_groups
from omsync.web.deps import get_db
from omsync.web.fragments import render_block
from omsync.web.routes.settings_wizard import check_files
from omsync.web.routes.shell import shell_context
from omsync.web.settings_views import (
    SETTINGS_NAV,
    InstitutionGroupView,
    ProviderCardView,
    VendorCardView,
    institution_group_view,
    provider_card_view,
    providers_summary,
    vendor_card_view,
)
from omsync.web.sync_runner import ALL, BANKS, RECEIPTS, JobPhase, SyncRefused
from omsync.web.sync_views import sync_status_view
from omsync.web.vault_logins import (
    VaultActionError,
    resolve_vault_login_name,
    search_logins,
    unlock_vault,
    vault_login_name,
    vault_status_view,
)

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/settings_providers.html"
FRAGMENTS = "settings/provider_fragments.html"
PATH = "/settings/sync"
UNLOCKED_POLLS = 3  # the sync polls that keep "Vault unlocked" in view, about six seconds


@router.get("/settings/sync", response_class=HTMLResponse)
@router.get("/settings/providers", response_class=HTMLResponse)
def providers_page(request: Request, db: DbSession, tab: str = "banks") -> HTMLResponse:
    context = shell_context(request, db, active="", title="Data sync")
    groups = _group_views(request, db)
    context.update(
        nav=SETTINGS_NAV,
        active_href=PATH,
        page_title="Data sync",
        summary=providers_summary(groups),
        groups=groups,
        vendor_cards=_vendor_views(request, db),
        sync=sync_status_view(request.app.state.sync_runner.view()),
        vault_status=vault_status_view(request.app.state.vault),
        vault_email=request.app.state.settings.email or "",
        tab=tab if tab in ("banks", "vendors") else "banks",
    )
    return request.app.state.templates.TemplateResponse(request, PAGE, context)


# Syncing from the page: a background job, its status region, and the question it may ask


@router.post(f"{PATH}/sync_all", response_class=HTMLResponse)
@router.post("/settings/providers/sync_all", response_class=HTMLResponse)
def sync_all_route(request: Request, db: DbSession) -> HTMLResponse:
    return _sync_request(request, db, ALL)


@router.post(f"{PATH}/sync_banks", response_class=HTMLResponse)
@router.post("/settings/providers/sync_banks", response_class=HTMLResponse)
def sync_banks_route(request: Request, db: DbSession) -> HTMLResponse:
    return _sync_request(request, db, BANKS)


@router.post(f"{PATH}/sync_receipts", response_class=HTMLResponse)
@router.post("/settings/providers/sync_receipts", response_class=HTMLResponse)
def sync_receipts_route(request: Request, db: DbSession) -> HTMLResponse:
    return _sync_request(request, db, RECEIPTS)


@router.post(f"{PATH}/vendor/{{vendor_key}}/sync", response_class=HTMLResponse)
@router.post("/settings/providers/vendor/{vendor_key}/sync", response_class=HTMLResponse)
def sync_vendor_route(request: Request, db: DbSession, vendor_key: str) -> HTMLResponse:
    return _sync_request(request, db, f"vendor:{vendor_key}")


@router.post(f"{PATH}/{{provider_key}}/sync", response_class=HTMLResponse)
@router.post("/settings/providers/{provider_key}/sync", response_class=HTMLResponse)
def sync_provider_route(request: Request, db: DbSession, provider_key: str) -> HTMLResponse:
    if provider_key in (ALL, BANKS, RECEIPTS):
        raise HTTPException(status_code=404, detail="No such provider")
    return _sync_request(request, db, provider_key)


@router.post(f"{PATH}/sync/unlock", response_class=HTMLResponse)
@router.post("/settings/providers/sync/unlock", response_class=HTMLResponse)
def sync_unlock_route(
    request: Request,
    db: DbSession,
    scope: Annotated[str, Form()],
    password: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Unlock the vault for the sync its form was shown for, then start that sync. The password is
    not echoed, stored, or logged. A failure shows the unlock form again with the vault's
    message."""
    bank_keys, vendor_keys, refusal, _ = _sync_plan(
        db, scope, has_vault=request.app.state.vault is not None
    )
    if refusal is not None:
        return _sync_reply(request, db, status=422, notice=refusal)
    try:
        unlock_vault(request.app.state.vault, request.app.state.login_limiter, password)
    except VaultActionError as exc:
        return _sync_reply(request, db, status=422, unlock_scope=scope, unlock_message=exc.message)
    return _start_sync(request, db, scope, bank_keys, vendor_keys, unlocked=UNLOCKED_POLLS)


@router.get(f"{PATH}/sync/status", response_class=HTMLResponse)
@router.get("/settings/providers/sync/status", response_class=HTMLResponse)
def sync_status_route(
    request: Request, db: DbSession, shown: int = 0, unlocked: int = 0
) -> HTMLResponse:
    """The status region, as a poll sends it. shown is the question the page already displays, so
    the question form is sent again only when it has changed. unlocked counts down the polls that
    still say the vault was just unlocked."""
    return _sync_reply(request, db, poll=True, shown=shown, unlocked=max(unlocked - 1, 0))


@router.get(f"{PATH}/vault-status", response_class=HTMLResponse)
@router.get("/settings/providers/vault-status", response_class=HTMLResponse)
def vault_status_route(request: Request) -> HTMLResponse:
    """The vault's status lines, which the Bank sync page polls: the vault locks itself after its
    idle timeout without any request, so the page would otherwise go on saying it is unlocked."""
    return _fragment(
        request, "frag_vault_status", vault_status=vault_status_view(request.app.state.vault)
    )


@router.post(f"{PATH}/sync/answer", response_class=HTMLResponse)
@router.post("/settings/providers/sync/answer", response_class=HTMLResponse)
def sync_answer_route(
    request: Request, db: DbSession, answer: Annotated[str, Form()] = ""
) -> HTMLResponse:
    """Give the answer to the question a sync is waiting on. The answer is never echoed back."""
    try:
        request.app.state.sync_runner.answer(answer)
    except SyncRefused as exc:
        return _sync_reply(request, db, status=422, notice=exc.message)
    return _sync_reply(request, db)


@router.post(f"{PATH}/sync/cancel", response_class=HTMLResponse)
@router.post("/settings/providers/sync/cancel", response_class=HTMLResponse)
def sync_cancel_route(request: Request, db: DbSession) -> HTMLResponse:
    """Cancel the running sync, one provider's or Sync all. The region keeps polling until the bank
    in progress has stopped."""
    try:
        request.app.state.sync_runner.cancel()
    except SyncRefused as exc:
        return _sync_reply(request, db, status=422, notice=exc.message)
    return _sync_reply(request, db)


# Editing a provider: its vault login and its accounts


@router.get(f"{PATH}/{{provider_key}}/edit/logins", response_class=HTMLResponse)
def search_edit_logins(
    request: Request, db: DbSession, provider_key: str, q: str = ""
) -> HTMLResponse:
    """The login results for a search in one provider's edit form. A locked vault shows the unlock
    form in their place. Nothing is saved by a search."""
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
        unlock_message="",
        message=message,
    )


@router.post(f"{PATH}/{{provider_key}}/edit/unlock", response_class=HTMLResponse)
def unlock_for_edit(
    request: Request,
    db: DbSession,
    provider_key: str,
    password: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Unlock the vault with the master password, then show the edit form again. The password is not
    echoed, stored, or logged, and a failure shows the vault's own message."""
    vault = request.app.state.vault
    unlock_message = ""
    try:
        unlock_vault(vault, request.app.state.login_limiter, password)
    except VaultActionError as exc:
        unlock_message = exc.message
    card = _card(request, db, provider_key)
    return _fragment(
        request,
        "frag_provider_edit",
        status=422 if unlock_message else 200,
        card=card,
        locked=is_locked(vault),
        logins=None,
        query="",
        unlock_message=unlock_message,
        message="",
        vault_status=vault_status_view(vault),
    )


@router.post(f"{PATH}/{{provider_key}}/edit", response_class=HTMLResponse)
def edit_route(
    request: Request,
    db: DbSession,
    provider_key: str,
    idempotency_key: Annotated[str, Form()] = "",
    vault_item_id: Annotated[str, Form()] = "",
    account: Annotated[list[str] | None, Form()] = None,
) -> HTMLResponse:
    """Save a provider's vault login and the accounts it fetches, as one batch. Only the accounts
    checked in the form are kept, and a login is changed only when one was chosen from a search."""
    try:
        batch = edit_provider(
            db,
            request.app.state.settings,
            provider_key=provider_key,
            login_id=vault_item_id or None,
            account_ids=account or [],
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except ProviderSetupError as exc:
        return _fragment(
            request,
            "frag_provider_message",
            status=409 if exc.conflict else 422,
            message=str(exc),
            error=True,
        )
    if batch is None:
        return _fragment(request, "frag_provider_message", message="Nothing changed.", error=False)
    return _saved(request, db, batch, message=f"Saved {provider_key}.")


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
        return _fragment(request, "frag_provider_message", status=422, message=str(exc), error=True)
    except BatchError:
        return _fragment(
            request,
            "frag_provider_message",
            status=409,
            message="This changed elsewhere, reload.",
            error=True,
        )
    return _saved(request, db, batch, message=str((batch.summary or {}).get("label", "Renamed.")))


# Deleting a provider


@router.post(f"{PATH}/{{provider_key}}/delete", response_class=HTMLResponse)
def delete_route(
    request: Request,
    db: DbSession,
    provider_key: str,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Delete a provider and its links to accounts, as one batch. Its accounts and statements
    stay."""
    try:
        batch = remove_provider(
            db,
            request.app.state.settings,
            provider_key=provider_key,
            idempotency_key=idempotency_key or uuid4().hex,
        )
    except ProviderSetupError as exc:
        return _fragment(
            request,
            "frag_provider_message",
            status=409 if exc.conflict else 422,
            message=str(exc),
            error=True,
        )
    return _saved(
        request,
        db,
        batch,
        message=(
            f"Deleted {provider_key}. Its accounts and statements are kept. "
            f"To undo, press Undo or run omsync batches undo {batch.id}."
        ),
    )


def provider_undo_reply(request: Request, db: Session) -> HTMLResponse:
    """After an undo of a provider batch: the toast, and the list re-read out of band."""
    return _fragment(
        request,
        "frag_provider_undone",
        message="Undone",
        groups=_group_views(request, db),
        panel=account_groups(db),
    )


# Editing a vendor profile


@router.get(f"{PATH}/vendor/{{vendor_key}}/edit/logins", response_class=HTMLResponse)
@router.get("/settings/providers/vendor/{vendor_key}/edit/logins", response_class=HTMLResponse)
def search_vendor_logins(
    request: Request, db: DbSession, vendor_key: str, q: str = ""
) -> HTMLResponse:
    card = _vendor_card(request, db, vendor_key)
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
        "frag_vendor_vault",
        card=card,
        locked=is_locked(vault),
        logins=logins,
        unlock_message="",
        message=message,
    )


@router.post(f"{PATH}/vendor/{{vendor_key}}/edit/unlock", response_class=HTMLResponse)
@router.post("/settings/providers/vendor/{vendor_key}/edit/unlock", response_class=HTMLResponse)
def unlock_for_vendor_edit(
    request: Request,
    db: DbSession,
    vendor_key: str,
    password: Annotated[str, Form()] = "",
) -> HTMLResponse:
    vault = request.app.state.vault
    unlock_message = ""
    try:
        unlock_vault(vault, request.app.state.login_limiter, password)
    except VaultActionError as exc:
        unlock_message = exc.message
    card = _vendor_card(request, db, vendor_key)
    return _fragment(
        request,
        "frag_vendor_edit",
        status=422 if unlock_message else 200,
        card=card,
        locked=is_locked(vault),
        logins=None,
        query="",
        unlock_message=unlock_message,
        message="",
        vault_status=vault_status_view(vault),
    )


@router.post(f"{PATH}/vendor/{{vendor_key}}/edit", response_class=HTMLResponse)
@router.post("/settings/providers/vendor/{vendor_key}/edit", response_class=HTMLResponse)
def edit_vendor_route(
    request: Request,
    db: DbSession,
    vendor_key: str,
    name: Annotated[str, Form()] = "",
    vault_item_id: Annotated[str, Form()] = "",
) -> HTMLResponse:
    profile = get_vendor_profile(db, vendor_key)
    if profile is None:
        raise HTTPException(status_code=404, detail="No such receipt vendor")
    clean_name = name.strip() or profile.name
    new_vault_id = vault_item_id.strip() if vault_item_id != "" else profile.vault_item_id
    updated = replace(
        profile,
        name=clean_name,
        vault_item_id=new_vault_id or None,
    )
    save_vendor_profile(db, updated)
    return _fragment(
        request,
        "frag_vendor_saved",
        vendor_cards=_vendor_views(request, db),
        message=f"Saved {updated.name}.",
    )


@router.post(f"{PATH}/vendor/{{vendor_key}}/delete", response_class=HTMLResponse)
@router.post("/settings/providers/vendor/{vendor_key}/delete", response_class=HTMLResponse)
def delete_vendor_route(
    request: Request,
    db: DbSession,
    vendor_key: str,
) -> HTMLResponse:
    profile = get_vendor_profile(db, vendor_key)
    if profile is None:
        raise HTTPException(status_code=404, detail="No such receipt vendor")
    delete_vendor_profile(db, vendor_key)
    return _fragment(
        request,
        "frag_vendor_saved",
        vendor_cards=_vendor_views(request, db),
        message=f"Removed {profile.name}. Receipts stay on disk.",
    )


# Helpers


def _saved(request: Request, db: Session, batch: Batch, *, message: str) -> HTMLResponse:
    return _fragment(
        request,
        "frag_provider_saved",
        groups=_group_views(request, db),
        panel=account_groups(db),
        message=message,
        batch_id=batch.id,
    )


def _sync_plan(
    db: Session, scope: str, *, has_vault: bool
) -> tuple[list[str], list[str], str | None, bool]:
    """The provider keys and vendor keys a sync of scope runs, why it cannot start,
    and whether it needs vault access."""
    if scope == ALL:
        bank_keys = enabled_provider_keys(db)
        vendor_keys = [p.key for p in list_vendor_profiles(db)]
        if not bank_keys and not vendor_keys:
            return [], [], "Nothing is turned on to sync.", False
        needs_vault = has_vault and bool(bank_keys)
        return bank_keys, vendor_keys, None, needs_vault

    if scope == BANKS:
        bank_keys = enabled_provider_keys(db)
        if not bank_keys:
            return [], [], "No bank provider is turned on, so there is nothing to sync.", False
        return bank_keys, [], None, has_vault

    if scope == RECEIPTS:
        vendor_keys = [p.key for p in list_vendor_profiles(db)]
        if not vendor_keys:
            return [], [], "No receipt vendor configured, so there is nothing to fetch.", False
        needs_vault = has_vault and any(
            bool((p := get_vendor_profile(db, k)) and p.vault_item_id) for k in vendor_keys
        )
        return [], vendor_keys, None, needs_vault

    if scope.startswith("vendor:"):
        v_key = scope.removeprefix("vendor:")
        profile = get_vendor_profile(db, v_key)
        if profile is None:
            raise HTTPException(status_code=404, detail="No such receipt vendor")
        needs_vault = bool(has_vault and profile.vault_item_id)
        return [], [v_key], None, needs_vault

    found = provider_group(db, scope)
    if found is None:
        raise HTTPException(status_code=404, detail="No such provider")
    refusal = sync_refusal(found[1], has_vault=has_vault)
    return [scope], [], refusal, has_vault


def _sync_request(request: Request, db: Session, scope: str) -> HTMLResponse:
    """Start a sync, or show why it cannot start. A locked vault shows the unlock form instead, and
    the sync starts once that form succeeds."""
    bank_keys, vendor_keys, refusal, needs_vault = _sync_plan(
        db, scope, has_vault=request.app.state.vault is not None
    )
    if refusal is not None:
        return _sync_reply(request, db, status=422, notice=refusal)
    if needs_vault and is_locked(request.app.state.vault):
        return _sync_reply(request, db, unlock_scope=scope)
    return _start_sync(request, db, scope, bank_keys, vendor_keys)


def _start_sync(
    request: Request,
    db: Session,
    scope: str,
    bank_keys: list[str],
    vendor_keys: list[str],
    *,
    unlocked: int = 0,
) -> HTMLResponse:
    try:
        request.app.state.sync_runner.start(scope, bank_keys, vendor_keys)
    except SyncRefused as exc:
        return _sync_reply(request, db, status=422, notice=exc.message)
    return _sync_reply(request, db, unlocked=unlocked)


def _sync_reply(
    request: Request,
    db: Session,
    *,
    status: int = 200,
    notice: str = "",
    unlock_scope: str | None = None,
    unlock_message: str = "",
    poll: bool = False,
    shown: int = 0,
    unlocked: int = 0,
) -> HTMLResponse:
    """The status region. While a job runs, a poll sends only the region and, when the question or
    notice changed, the question slot, so the buttons and the list are not redrawn each time.
    Otherwise the buttons, the provider list and the user menus' vault status go out of band too,
    so they show the job's state. unlocked is the polls left that say the vault was just
    unlocked."""
    sync = sync_status_view(request.app.state.sync_runner.view())
    controls = not (poll and sync.running)
    return _fragment(
        request,
        "frag_sync",
        status=status,
        sync=sync,
        notice=notice,
        unlock_scope=unlock_scope,
        unlock_message=unlock_message,
        unlocked=unlocked if sync.running else 0,
        include_question=not poll or shown != sync.shown_number,
        groups=_group_views(request, db) if controls else None,
        vendor_cards=_vendor_views(request, db) if controls else None,
        vault_status=vault_status_view(request.app.state.vault),
        poll=poll,
    )


def _vendor_views(request: Request, db: Session) -> list[VendorCardView]:
    vault = request.app.state.vault
    names = request.app.state.login_names
    locked = is_locked(vault)
    busy = request.app.state.sync_runner.view().phase is JobPhase.running
    settings = request.app.state.settings
    profiles = list_vendor_profiles(db)
    latest_runs = latest_sync_runs(db, kind=SyncKind.receipt)
    views: list[VendorCardView] = []
    for profile in profiles:
        hints = (profile.name, profile.vendor_key, profile.key)
        vault_text = resolve_vault_login_name(vault, profile.vault_item_id, names, hints)
        count = vendor_receipt_count(settings, profile)
        latest = latest_receipt_date(settings, profile)
        recent = latest_runs.get(profile.key)
        sync = sync_views([recent])[0] if recent is not None else None
        synced_at = recent.started_at if recent is not None else None
        views.append(
            vendor_card_view(
                profile,
                vault_text,
                count,
                latest,
                locked=locked,
                busy=busy,
                can_delete=True,
                sync=sync,
                synced_at=synced_at,
            )
        )
    return views


def _vendor_card(request: Request, db: Session, vendor_key: str) -> VendorCardView:
    profile = get_vendor_profile(db, vendor_key)
    if profile is None:
        raise HTTPException(status_code=404, detail="No such receipt vendor")
    vault = request.app.state.vault
    names = request.app.state.login_names
    hints = (profile.name, profile.vendor_key, profile.key)
    vault_text = resolve_vault_login_name(vault, profile.vault_item_id, names, hints)
    count = vendor_receipt_count(request.app.state.settings, profile)
    latest = latest_receipt_date(request.app.state.settings, profile)
    latest_runs = latest_sync_runs(db, kind=SyncKind.receipt)
    recent = latest_runs.get(profile.key)
    sync = sync_views([recent])[0] if recent is not None else None
    synced_at = recent.started_at if recent is not None else None
    return vendor_card_view(
        profile,
        vault_text,
        count,
        latest,
        locked=is_locked(vault),
        busy=request.app.state.sync_runner.view().phase is JobPhase.running,
        can_delete=True,
        sync=sync,
        synced_at=synced_at,
    )


def _group_views(request: Request, db: Session) -> list[InstitutionGroupView]:
    vault = request.app.state.vault
    names = request.app.state.login_names
    locked = is_locked(vault)
    busy = request.app.state.sync_runner.view().phase is JobPhase.running
    views: list[InstitutionGroupView] = []
    for group in bank_sync_groups(db):
        texts = {
            provider.key: vault_login_name(vault, provider, group, names)
            for provider in group.providers
        }
        views.append(institution_group_view(group, texts, locked, busy))
    return views


def _card(request: Request, db: Session, provider_key: str) -> ProviderCardView:
    found = provider_group(db, provider_key)
    if found is None:
        raise HTTPException(status_code=404, detail="No such provider")
    group, provider = found
    vault = request.app.state.vault
    return provider_card_view(
        group,
        provider,
        vault_login_name(vault, provider, group, request.app.state.login_names),
        locked=is_locked(vault),
        busy=request.app.state.sync_runner.view().phase is JobPhase.running,
    )


def _fragment(
    request: Request,
    block: str,
    *,
    status: int = 200,
    **context: object,
) -> HTMLResponse:
    return render_block(request, FRAGMENTS, block, status=status, **context)
