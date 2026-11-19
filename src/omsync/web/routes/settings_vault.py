"""Connect and disconnect the vault, from the panel of the vault button on the Bank sync page.

A connect unlocks the vault first, so a wrong email or master password is never saved. A success
reloads the page, because the provider cards, the wizard and the sync checks all change with it.
"""

from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from omsync.core.settings import Settings, update_settings
from omsync.core.vault import Vault, VaultError
from omsync.web.fragments import render_block
from omsync.web.sync_runner import SyncRefused

router = APIRouter()
PATH = "/settings/providers/vault"
FRAGMENTS = "settings/provider_fragments.html"
TOO_MANY_ATTEMPTS = "Too many failed attempts. Wait a minute, then try again."
DEMO_MESSAGE = "Demo: the vault cannot be changed."
ALREADY_CONNECTED = "A vault is connected. Disconnect it first."
EMPTY_EMAIL = "Enter your Bitwarden email."
NO_MASTER = "Enter your master password."


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
    if email is None:
        email = request.app.state.settings.email or ""
    return render_block(
        request, FRAGMENTS, "frag_vault_panel", status=422, message=message, email=email
    )
