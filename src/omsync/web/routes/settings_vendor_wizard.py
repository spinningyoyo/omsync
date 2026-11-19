"""The Add vendor wizard for receipt synchronization.

Guides setting up a merchant/vendor connector profile and its vault login.
"""

import re
from dataclasses import dataclass
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from omsync.core.vault import LoginSummary, Vault, is_locked
from omsync.receipts.connectors.registry import registered_connectors
from omsync.receipts.profiles import VendorProfile, get_vendor_profile, save_vendor_profile
from omsync.web.deps import get_db
from omsync.web.fragments import render_block
from omsync.web.routes.shell import shell_context
from omsync.web.settings_views import SETTINGS_NAV
from omsync.web.vault_logins import (
    VaultActionError,
    search_logins,
    unlock_vault,
    vault_status_view,
)

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/settings_vendor_wizard.html"
FRAGMENTS = "settings/vendor_wizard_fragments.html"
PROVIDERS_PATH = "/settings/sync"
WIZARD = "/settings/vendors/new"
MAX_FORM_VALUE = 200


@dataclass(frozen=True)
class VendorWizardState:
    vendor: str = ""
    name: str = ""
    vendor_key: str = ""


@router.get(WIZARD, response_class=HTMLResponse)
def vendor_wizard_page(request: Request, db: DbSession) -> HTMLResponse:
    context = shell_context(request, db, active="", title="Add vendor")
    vendors = registered_connectors()
    default_vendor = vendors[0].key if vendors else ""
    default_name = vendors[0].label if vendors else ""
    state = VendorWizardState(
        vendor=default_vendor,
        name=default_name,
        vendor_key=default_vendor,
    )
    context.update(
        nav=SETTINGS_NAV,
        active_href=PROVIDERS_PATH,
        page_title="Add vendor",
        state=state,
        vendors=vendors,
        form_key=uuid4().hex,
        message="",
    )
    return request.app.state.templates.TemplateResponse(request, PAGE, context)


@router.post(f"{WIZARD}/setup", response_class=HTMLResponse)
def choose_vendor(
    request: Request,
    db: DbSession,
    vendor: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    vendor_key: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Validate vendor selection and profile details, then advance to vault login."""
    key_value = idempotency_key or uuid4().hex
    clean_vendor = vendor.strip().lower()[:MAX_FORM_VALUE]
    clean_name = name.strip()[:MAX_FORM_VALUE]
    clean_key = vendor_key.strip().lower()[:MAX_FORM_VALUE]
    state = VendorWizardState(
        vendor=clean_vendor,
        name=clean_name,
        vendor_key=clean_key,
    )
    vendors = registered_connectors()
    known_keys = {c.key for c in vendors}
    if clean_vendor not in known_keys:
        return _step(
            request,
            "frag_step_setup",
            422,
            **_setup_context(state, vendors, key_value, "Choose a vendor from the list."),
        )
    if not clean_name:
        return _step(
            request,
            "frag_step_setup",
            422,
            **_setup_context(state, vendors, key_value, "Enter a display name."),
        )
    if not clean_key:
        return _step(
            request,
            "frag_step_setup",
            422,
            **_setup_context(state, vendors, key_value, "Enter a vendor key."),
        )
    if not re.fullmatch(r"[a-z0-9][a-z0-9_\-]*", clean_key):
        return _step(
            request,
            "frag_step_setup",
            422,
            **_setup_context(
                state,
                vendors,
                key_value,
                "Vendor key must use lowercase letters, numbers, hyphens or underscores.",
            ),
        )
    if get_vendor_profile(db, clean_key) is not None:
        return _step(
            request,
            "frag_step_setup",
            422,
            **_setup_context(
                state,
                vendors,
                key_value,
                f"A vendor profile with key {clean_key!r} already exists.",
            ),
        )

    vault = request.app.state.vault
    locked = is_locked(vault)
    logins, query = _initial_vault_logins(request, vault, state, locked=locked)
    return _step(
        request,
        "frag_step_login",
        **_login_context(
            state,
            locked=locked,
            form_key=uuid4().hex,
            logins=logins,
            query=query,
        ),
    )


@router.post(f"{WIZARD}/back", response_class=HTMLResponse)
def back_to_setup(
    request: Request,
    vendor: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    vendor_key: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Return to step 1 to edit vendor setup details."""
    vendors = registered_connectors()
    state = VendorWizardState(
        vendor=vendor.strip().lower()[:MAX_FORM_VALUE],
        name=name.strip()[:MAX_FORM_VALUE],
        vendor_key=vendor_key.strip().lower()[:MAX_FORM_VALUE],
    )
    return _step(
        request,
        "frag_step_setup",
        **_setup_context(state, vendors, idempotency_key or uuid4().hex),
    )


@router.get(f"{WIZARD}/logins", response_class=HTMLResponse)
def search_vault(
    request: Request,
    vendor: str = "",
    name: str = "",
    vendor_key: str = "",
    q: str = "",
) -> HTMLResponse:
    """The debounced login results for vault search."""
    vault = request.app.state.vault
    state = VendorWizardState(
        vendor=vendor[:MAX_FORM_VALUE],
        name=name[:MAX_FORM_VALUE],
        vendor_key=vendor_key[:MAX_FORM_VALUE],
    )
    query = q.strip()
    if is_locked(vault):
        return _step(
            request,
            "frag_vault_results",
            state=state,
            locked=True,
            logins=None,
            unlock_message="",
            message="",
        )
    if not query:
        logins, _ = _initial_vault_logins(request, vault, state, locked=False)
        return _step(
            request,
            "frag_vault_results",
            state=state,
            locked=False,
            logins=logins,
            unlock_message="",
            message="",
        )
    try:
        results = search_logins(vault, query, request.app.state.login_names)
    except VaultActionError as exc:
        return _step(
            request,
            "frag_vault_results",
            state=state,
            locked=False,
            logins=None,
            unlock_message="",
            message=exc.message,
        )
    return _step(
        request,
        "frag_vault_results",
        state=state,
        locked=False,
        logins=results,
        unlock_message="",
        message="" if results else "No logins found.",
    )


@router.post(f"{WIZARD}/unlock", response_class=HTMLResponse)
def unlock(
    request: Request,
    password: Annotated[str, Form()] = "",
    vendor: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    vendor_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Unlock the vault inline and show search results."""
    vault = request.app.state.vault
    limiter = request.app.state.login_limiter
    state = VendorWizardState(
        vendor=vendor[:MAX_FORM_VALUE],
        name=name[:MAX_FORM_VALUE],
        vendor_key=vendor_key[:MAX_FORM_VALUE],
    )
    try:
        unlock_vault(vault, limiter, password)
    except VaultActionError as exc:
        return _step(
            request,
            "frag_step_login",
            422,
            **_login_context(
                state,
                locked=True,
                form_key=uuid4().hex,
                unlock_message=exc.message,
            ),
        )
    logins, query = _initial_vault_logins(request, vault, state, locked=False)
    return _step(
        request,
        "frag_step_login",
        **_login_context(
            state,
            locked=False,
            form_key=uuid4().hex,
            logins=logins,
            query=query,
        ),
        vault_status=vault_status_view(vault),
    )


@router.post(f"{WIZARD}/login", response_class=Response)
def save_vendor(
    request: Request,
    db: DbSession,
    vendor: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    vendor_key: Annotated[str, Form()] = "",
    vault_item_id: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> Response:
    """Save the vendor profile and redirect to the Vendors tab on Data sync."""
    clean_vendor = vendor.strip().lower()[:MAX_FORM_VALUE]
    clean_name = name.strip()[:MAX_FORM_VALUE]
    clean_key = vendor_key.strip().lower()[:MAX_FORM_VALUE]
    state = VendorWizardState(
        vendor=clean_vendor,
        name=clean_name,
        vendor_key=clean_key,
    )
    if get_vendor_profile(db, clean_key) is not None:
        vault = request.app.state.vault
        locked = is_locked(vault)
        logins, query = _initial_vault_logins(request, vault, state, locked=locked)
        return _step(
            request,
            "frag_step_login",
            422,
            **_login_context(
                state,
                locked=locked,
                form_key=idempotency_key or uuid4().hex,
                logins=logins,
                query=query,
                message=f"A vendor profile with key {clean_key!r} already exists.",
            ),
        )
    profile = VendorProfile(
        key=clean_key,
        vendor_key=clean_vendor,
        name=clean_name,
        account=clean_key,
        vault_item_id=vault_item_id or None,
    )
    save_vendor_profile(db, profile)
    return Response(status_code=204, headers={"HX-Redirect": "/settings/sync?tab=vendors"})


def _initial_vault_logins(
    request: Request,
    vault: Vault | None,
    state: VendorWizardState,
    *,
    locked: bool,
) -> tuple[list[LoginSummary] | None, str]:
    if vault is None or locked:
        return None, ""
    for candidate in (state.name, state.vendor_key, state.vendor):
        query = candidate.strip()
        if not query:
            continue
        try:
            logins = search_logins(vault, query, request.app.state.login_names)
        except VaultActionError:
            continue
        if logins:
            return logins, query
    query = (state.name or state.vendor_key or state.vendor).strip()
    return None, query


def _setup_context(
    state: VendorWizardState,
    vendors: list[object],
    form_key: str,
    message: str = "",
) -> dict[str, object]:
    return {
        "state": state,
        "vendors": vendors,
        "form_key": form_key,
        "message": message,
    }


def _login_context(
    state: VendorWizardState,
    *,
    locked: bool,
    form_key: str,
    logins: list[LoginSummary] | None = None,
    query: str = "",
    message: str = "",
    unlock_message: str = "",
) -> dict[str, object]:
    return {
        "state": state,
        "locked": locked,
        "logins": logins,
        "query": query,
        "form_key": form_key,
        "message": message,
        "unlock_message": unlock_message,
    }


def _step(
    request: Request,
    block_name: str,
    status: int = 200,
    **context: object,
) -> HTMLResponse:
    return render_block(request, FRAGMENTS, block_name, status=status, **context)
