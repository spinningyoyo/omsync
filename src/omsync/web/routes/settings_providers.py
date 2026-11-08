"""Bank sync: each institution's providers and the accounts they fetch, and editing and deleting a
provider as batches. Adding one is the wizard in settings_wizard.py."""

from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import BatchError
from omsync.ingest.connectors.config import enabled_provider_keys
from omsync.ingest.institutions import bank_sync_groups, provider_group, sync_refusal
from omsync.ingest.providers import ProviderSetupError, edit_provider, remove_provider
from omsync.ledger.accounts_view import update_account
from omsync.ledger.preferences import SYNC_CLOSE_ON_FAILURE, close_browser_on_failure, set_pref
from omsync.web.accounts_panel import account_groups
from omsync.web.deps import get_db
from omsync.web.routes.settings_wizard import check_files
from omsync.web.routes.shell import shell_context
from omsync.web.settings_views import (
    SETTINGS_NAV,
    InstitutionGroupView,
    ProviderCardView,
    institution_group_view,
    provider_card_view,
)
from omsync.web.sync_runner import ALL, JobPhase, SyncRefused
from omsync.web.sync_views import sync_status_view
from omsync.web.vault_logins import (
    VaultActionError,
    search_logins,
    unlock_vault,
    vault_login_name,
)

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/settings_providers.html"
FRAGMENTS = "settings/provider_fragments.html"
PATH = "/settings/providers"


@router.get("/settings/providers", response_class=HTMLResponse)
def providers_page(request: Request, db: DbSession) -> HTMLResponse:
    context = shell_context(request, db, active="", title="Bank sync")
    context.update(
        nav=SETTINGS_NAV,
        active_href=PATH,
        page_title="Bank sync",
        groups=_group_views(request, db),
        sync=sync_status_view(request.app.state.sync_runner.view()),
        close_on_failure=close_browser_on_failure(db),
    )
    return request.app.state.templates.TemplateResponse(request, PAGE, context)


# Syncing from the page: a background job, its status region, and the question it may ask


@router.post(f"{PATH}/sync/close-on-failure", status_code=204)
def set_close_on_failure(db: DbSession, close: Annotated[str, Form()] = "") -> Response:
    """Save whether a failed sync closes Chrome. An unticked checkbox sends nothing."""
    set_pref(db, SYNC_CLOSE_ON_FAILURE, close == "1")
    return Response(status_code=204)


@router.post(f"{PATH}/sync_all", response_class=HTMLResponse)
def sync_all_route(request: Request, db: DbSession) -> HTMLResponse:
    return _sync_request(request, db, ALL)


@router.post(f"{PATH}/{{provider_key}}/sync", response_class=HTMLResponse)
def sync_provider_route(request: Request, db: DbSession, provider_key: str) -> HTMLResponse:
    if provider_key == ALL:
        raise HTTPException(status_code=404, detail="No such provider")
    return _sync_request(request, db, provider_key)


@router.post(f"{PATH}/sync/unlock", response_class=HTMLResponse)
def sync_unlock_route(
    request: Request,
    db: DbSession,
    scope: Annotated[str, Form()],
    password: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Unlock the vault for the sync its form was shown for, then start that sync. The password is
    not echoed, stored, or logged. A failure shows the unlock form again with the vault's
    message."""
    keys, refusal = _sync_plan(db, scope)
    if refusal is not None:
        return _sync_reply(request, db, status=422, notice=refusal)
    try:
        unlock_vault(
            request.app.state.vault,
            request.app.state.login_limiter,
            request.state.session.email,
            password,
        )
    except VaultActionError as exc:
        return _sync_reply(request, db, status=422, unlock_scope=scope, unlock_message=exc.message)
    return _start_sync(request, db, scope, keys)


@router.get(f"{PATH}/sync/status", response_class=HTMLResponse)
def sync_status_route(request: Request, db: DbSession, shown: int = 0) -> HTMLResponse:
    """The status region, as a poll sends it. shown is the question the page already displays, so
    the question form is sent again only when it has changed."""
    return _sync_reply(request, db, poll=True, shown=shown)


@router.post(f"{PATH}/sync/answer", response_class=HTMLResponse)
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
    if vault.is_unlocked() and query:
        try:
            logins = search_logins(vault, query, request.app.state.login_names)
        except VaultActionError as exc:
            message = exc.message
    return _fragment(
        request,
        "frag_provider_vault",
        key=provider_key,
        locked=not vault.is_unlocked(),
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
        unlock_vault(vault, request.app.state.login_limiter, request.state.session.email, password)
    except VaultActionError as exc:
        unlock_message = exc.message
    card = _card(request, db, provider_key)
    return _fragment(
        request,
        "frag_provider_edit",
        status=422 if unlock_message else 200,
        card=card,
        locked=not vault.is_unlocked(),
        logins=None,
        query="",
        unlock_message=unlock_message,
        message="",
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


def _sync_plan(db: Session, scope: str) -> tuple[list[str], str | None]:
    """The provider keys a sync of scope runs, and why it cannot start, or None when it can. An
    unknown provider is a 404."""
    if scope == ALL:
        keys = enabled_provider_keys(db)
        return keys, None if keys else "No provider is turned on, so there is nothing to sync."
    found = provider_group(db, scope)
    if found is None:
        raise HTTPException(status_code=404, detail="No such provider")
    return [scope], sync_refusal(found[1])


def _sync_request(request: Request, db: Session, scope: str) -> HTMLResponse:
    """Start a sync, or show why it cannot start. A locked vault shows the unlock form instead, and
    the sync starts once that form succeeds."""
    keys, refusal = _sync_plan(db, scope)
    if refusal is not None:
        return _sync_reply(request, db, status=422, notice=refusal)
    if not request.app.state.vault.is_unlocked():
        return _sync_reply(request, db, unlock_scope=scope)
    return _start_sync(request, db, scope, keys)


def _start_sync(request: Request, db: Session, scope: str, keys: list[str]) -> HTMLResponse:
    try:
        request.app.state.sync_runner.start(scope, keys)
    except SyncRefused as exc:
        return _sync_reply(request, db, status=422, notice=exc.message)
    return _sync_reply(request, db)


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
) -> HTMLResponse:
    """The status region. While a job runs, a poll sends only the region and, when the question or
    notice changed, the question slot, so the buttons and the list are not redrawn each time.
    Otherwise the buttons and the provider list go out of band too, so they show the job's
    state."""
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
        include_question=not poll or shown != sync.shown_number,
        groups=_group_views(request, db) if controls else None,
    )


def _group_views(request: Request, db: Session) -> list[InstitutionGroupView]:
    vault = request.app.state.vault
    names = request.app.state.login_names
    locked = not vault.is_unlocked()
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
        locked=not vault.is_unlocked(),
        busy=request.app.state.sync_runner.view().phase is JobPhase.running,
    )


def _fragment(
    request: Request,
    block: str,
    *,
    status: int = 200,
    **context: object,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, FRAGMENTS, context, status_code=status, block_name=block
    )
