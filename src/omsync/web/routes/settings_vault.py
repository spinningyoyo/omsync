"""The vault button's panel: connect, unlock, lock and disconnect Bitwarden, and the status line
the page polls.

A connect unlocks the vault first, so a wrong email or master password is never saved. A connect
or disconnect reloads the page, because the provider cards, the wizards and the sync checks all
change with it. A sync that needs the vault while it is locked answers with prompt_unlock, which
opens this panel on its unlock form and starts the sync once the vault is unlocked. A login search
shows its own unlock form instead, because it can sit in a dialog that covers the panel.
"""

import json
from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from omsync.core.settings import Settings, update_settings
from omsync.core.vault import Vault, VaultError
from omsync.sync.run import plan_sync
from omsync.web.deps import get_db
from omsync.web.fragments import render_block
from omsync.web.sync_runner import SyncRefused
from omsync.web.vault_logins import VaultActionError, unlock_vault, vault_status_view

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PATH = "/settings/sync/vault"
FRAGMENTS = "settings/provider_fragments.html"
TOO_MANY_ATTEMPTS = "Too many failed attempts. Wait a minute, then try again."
DEMO_MESSAGE = "Demo: the vault cannot be changed."
ALREADY_CONNECTED = "A vault is connected. Disconnect it first."
EMPTY_EMAIL = "Enter your Bitwarden email."
NO_MASTER = "Enter your master password."
SYNC_PENDING = "sync:"  # what to do after an unlock: start the sync of the scope that follows
INLINE = "inline"  # the unlock form that stands in for a locked vault's login results
UNLOCK_EVENT = "vault-unlock"  # asks the page to open the panel
UNLOCKED_EVENT = "vault-unlocked"  # tells the page the vault was unlocked


@router.post(f"{PATH}/connect", response_class=HTMLResponse)
def connect_vault(
    request: Request,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    state = request.app.state
    email = email.strip()
    if state.demo is not None:
        return _refused(request, DEMO_MESSAGE, email)
    if state.vault is not None:
        return _refused(request, ALREADY_CONNECTED)
    if state.login_limiter.is_blocked():
        return _refused(request, TOO_MANY_ATTEMPTS, email)
    if not email:
        return _refused(request, EMPTY_EMAIL, email)
    if not password:
        return _refused(request, NO_MASTER, email)

    old: Settings = state.settings
    new = replace(old, vault="bitwarden", email=email)
    vault = state.vault_opener(new)
    try:
        vault.unlock(password)
    except VaultError as exc:
        state.login_limiter.record_failure()
        return _refused(request, str(exc), email)
    state.login_limiter.reset()
    try:
        state.sync_runner.use_vault(vault, new)
    except SyncRefused as exc:
        vault.lock()
        return _refused(request, exc.message, email)
    try:
        update_settings({"vault": "bitwarden", "email": email})
    except (ValueError, OSError) as exc:
        state.sync_runner.use_vault(None, old)
        vault.lock()
        return _refused(request, str(exc), email)
    _use(request, vault, new)
    return _reload()


@router.post(f"{PATH}/disconnect", response_class=HTMLResponse)
def disconnect_vault(request: Request) -> Response:
    state = request.app.state
    if state.demo is not None:
        return _refused(request, DEMO_MESSAGE)
    vault = state.vault
    old: Settings = state.settings
    new = replace(old, vault="none", email=None)
    try:
        state.sync_runner.use_vault(None, new)
    except SyncRefused as exc:
        return _refused(request, exc.message)
    try:
        update_settings({"vault": "none", "email": ""})
    except (ValueError, OSError) as exc:
        state.sync_runner.use_vault(vault, old)
        return _refused(request, str(exc))
    if vault is not None:
        vault.disconnect()
    _use(request, None, new)
    return _reload()


@router.post(f"{PATH}/unlock", response_class=HTMLResponse)
def unlock(
    request: Request,
    db: DbSession,
    password: Annotated[str, Form()] = "",
    pending: Annotated[str, Form()] = "",
    place: Annotated[str, Form()] = "",
    form_id: Annotated[str, Form()] = "",
) -> Response:
    """Unlock the vault, then do what the unlock was asked for: start a sync, or tell the page to
    search again. The password is not echoed, stored, or logged. A failure shows the form it came
    from again with the vault's message: the panel, or the form in place of login results."""
    state = request.app.state
    try:
        unlock_vault(state.vault, state.login_limiter, password)
    except VaultActionError as exc:
        if place == INLINE:
            return _inline(request, form_id, status=422, message=exc.message)
        return _panel(request, status=422, message=exc.message, pending=pending)
    if place == INLINE:
        reply = _inline(request, form_id)
        panel = _panel(request, oob=True)
        return HTMLResponse(
            reply.body + panel.body, headers={"HX-Trigger": json.dumps({UNLOCKED_EVENT: True})}
        )
    if pending.startswith(SYNC_PENDING):
        try:
            start_sync(request, db, pending.removeprefix(SYNC_PENDING))
        except SyncRefused as exc:
            return _panel(request, message=exc.message)
        return _reload()
    response = _panel(request)
    response.headers["HX-Trigger"] = json.dumps({UNLOCKED_EVENT: True})
    return response


@router.post(f"{PATH}/lock", response_class=HTMLResponse)
def lock(request: Request) -> HTMLResponse:
    state = request.app.state
    if state.vault is not None:
        state.vault.lock()
    state.login_names.clear()
    return _panel(request)


@router.get(f"{PATH}/status", response_class=HTMLResponse)
def status(request: Request) -> HTMLResponse:
    """The status line the page polls: the vault locks itself after its idle timeout without any
    request, so the page would otherwise go on saying it is unlocked. It reads memory only."""
    return render_block(
        request,
        FRAGMENTS,
        "frag_vault_status",
        vault_status=vault_status_view(request.app.state.vault),
    )


def prompt_unlock(request: Request, response: HTMLResponse, pending: str) -> HTMLResponse:
    """response with the vault panel on its unlock form added out of band, and an event that opens
    it. pending is what the unlock then does: SYNC_PENDING plus a scope."""
    panel = _panel(request, pending=pending, oob=True)
    return HTMLResponse(
        response.body + panel.body,
        status_code=response.status_code,
        headers={"HX-Trigger": json.dumps({UNLOCK_EVENT: True})},
    )


def start_sync(request: Request, db: Session, scope: str) -> None:
    """Start the sync of scope, or raise SyncRefused with why it cannot start."""
    try:
        plan = plan_sync(db, scope, has_vault=request.app.state.vault is not None)
    except LookupError:
        raise SyncRefused("There is no such provider.") from None
    if plan.refusal is not None:
        raise SyncRefused(plan.refusal)
    request.app.state.sync_runner.start(scope, plan.bank_keys, plan.receipt_keys)


def _use(request: Request, vault: Vault | None, settings: Settings) -> None:
    state = request.app.state
    state.vault = vault
    state.settings = settings
    state.login_names.clear()
    state.templates.env.globals["has_vault"] = vault is not None
    state.templates.env.cache.clear()


def _reload() -> Response:
    return Response(status_code=204, headers={"HX-Refresh": "true"})


def _refused(request: Request, message: str, email: str | None = None) -> HTMLResponse:
    """The panel again with message. email is what was typed; None shows the saved one."""
    return _panel(request, status=422, message=message, email=email)


def _inline(
    request: Request, form_id: str, *, status: int = 200, message: str = ""
) -> HTMLResponse:
    return render_block(
        request,
        FRAGMENTS,
        "frag_vault_unlock_inline",
        status=status,
        form_id=form_id,
        message=message,
    )


def _panel(
    request: Request,
    *,
    status: int = 200,
    message: str = "",
    email: str | None = None,
    pending: str = "",
    oob: bool = False,
) -> HTMLResponse:
    """The panel for the vault's state now, with the status line beside it."""
    state = request.app.state
    return render_block(
        request,
        FRAGMENTS,
        "frag_vault_panel",
        status=status,
        message=message,
        email=state.settings.email or "" if email is None else email,
        pending=pending,
        vault_status=vault_status_view(state.vault),
        oob=oob,
    )
