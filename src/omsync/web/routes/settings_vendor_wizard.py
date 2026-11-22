"""The wizard that adds a receipt provider.

Guides setting up a receipt provider: its store, its name, and, for a store that uses one, its
vault login. The provider's key is made from its name.
"""

from dataclasses import dataclass
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from omsync.core.vault import LoginSummary, Vault, is_locked
from omsync.ingest.connectors.config import ProviderError
from omsync.receipts.connectors.registry import registered_connectors
from omsync.receipts.profiles import VendorProfile, create_vendor_profile, new_profile_key
from omsync.web.deps import get_db
from omsync.web.fragments import render_block
from omsync.web.routes.shell import shell_context
from omsync.web.settings_views import SETTINGS_NAV
from omsync.web.vault_logins import VaultActionError, search_logins

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "pages/settings_vendor_wizard.html"
FRAGMENTS = "settings/vendor_wizard_fragments.html"
PROVIDERS_PATH = "/settings/sync"
WIZARD = "/settings/sync/new/receipt"
MAX_FORM_VALUE = 200


@dataclass(frozen=True)
class VendorWizardState:
    connector: str = ""
    name: str = ""


@router.get(WIZARD, response_class=HTMLResponse)
def vendor_wizard_page(request: Request, db: DbSession) -> HTMLResponse:
    context = shell_context(request, db, active="", title="Add receipts")
    connectors = registered_connectors()
    state = VendorWizardState(
        connector=connectors[0].key if connectors else "",
        name=connectors[0].label if connectors else "",
    )
    context.update(
        nav=SETTINGS_NAV,
        active_href=PROVIDERS_PATH,
        page_title="Add receipts",
        state=state,
        connectors=connectors,
        form_key=uuid4().hex,
        message="",
    )
    return request.app.state.templates.TemplateResponse(request, PAGE, context)


@router.post(f"{WIZARD}/connector", response_class=HTMLResponse)
def choose_connector(
    request: Request,
    db: DbSession,
    connector: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> Response:
    """Validate the store and name. A store that signs in with a vault login goes on to pick the
    login. Any other is added now."""
    key_value = idempotency_key or uuid4().hex
    state = _state(connector, name)
    connectors = registered_connectors()
    chosen = next((c for c in connectors if c.key == state.connector), None)
    if chosen is None:
        return _step(
            request,
            "frag_step_setup",
            422,
            **_setup_context(state, connectors, key_value, "Choose a store from the list."),
        )
    if not state.name:
        return _step(
            request,
            "frag_step_setup",
            422,
            **_setup_context(state, connectors, key_value, "Enter a display name."),
        )
    if not chosen.uses_vault:
        return _create(request, db, state, vault_item_id=None, idempotency_key=key_value)
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
    connector: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Return to step 1 to edit the store and name."""
    return _step(
        request,
        "frag_step_setup",
        **_setup_context(
            _state(connector, name), registered_connectors(), idempotency_key or uuid4().hex
        ),
    )


@router.get(f"{WIZARD}/logins", response_class=HTMLResponse)
def search_vault(
    request: Request,
    connector: str = "",
    name: str = "",
    q: str = "",
) -> HTMLResponse:
    """The debounced login results for vault search. A locked vault shows the unlock form in their
    place."""
    vault = request.app.state.vault
    state = _state(connector, name)
    query = q.strip()
    if is_locked(vault):
        return _step(
            request, "frag_vault_results", state=state, locked=True, logins=None, message=""
        )
    if not query:
        logins, _ = _initial_vault_logins(request, vault, state, locked=False)
        return _step(
            request,
            "frag_vault_results",
            state=state,
            locked=False,
            logins=logins,
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
            message=exc.message,
        )
    return _step(
        request,
        "frag_vault_results",
        state=state,
        locked=False,
        logins=results,
        message="" if results else "No logins found.",
    )


@router.post(f"{WIZARD}/login", response_class=Response)
def save_vendor(
    request: Request,
    db: DbSession,
    connector: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    vault_item_id: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> Response:
    """Save the receipt provider with its vault login and redirect to the Receipts tab."""
    return _create(
        request,
        db,
        _state(connector, name),
        vault_item_id=vault_item_id or None,
        idempotency_key=idempotency_key or uuid4().hex,
    )


def _state(connector: str, name: str) -> VendorWizardState:
    return VendorWizardState(
        connector=connector.strip().lower()[:MAX_FORM_VALUE], name=name.strip()[:MAX_FORM_VALUE]
    )


def _create(
    request: Request,
    db: Session,
    state: VendorWizardState,
    *,
    vault_item_id: str | None,
    idempotency_key: str,
) -> Response:
    """Add the provider, with a key made from its name, and go to the Receipts tab. A refusal
    shows on the step that asked."""
    profile = VendorProfile(
        key=new_profile_key(db, state.connector, state.name),
        vendor_key=state.connector,
        name=state.name,
        vault_item_id=vault_item_id,
    )
    try:
        create_vendor_profile(db, profile, idempotency_key=idempotency_key)
    except ProviderError as exc:
        return _refused(request, state, idempotency_key, str(exc))
    return Response(status_code=204, headers={"HX-Redirect": "/settings/sync?tab=receipts"})


def _refused(request: Request, state: VendorWizardState, form_key: str, message: str) -> Response:
    connector = next((c for c in registered_connectors() if c.key == state.connector), None)
    if connector is None or not connector.uses_vault:
        return _step(
            request,
            "frag_step_setup",
            422,
            **_setup_context(state, registered_connectors(), form_key, message),
        )
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
            form_key=form_key,
            logins=logins,
            query=query,
            message=message,
        ),
    )


def _initial_vault_logins(
    request: Request,
    vault: Vault | None,
    state: VendorWizardState,
    *,
    locked: bool,
) -> tuple[list[LoginSummary] | None, str]:
    if vault is None or locked:
        return None, ""
    for candidate in (state.name, state.connector):
        query = candidate.strip()
        if not query:
            continue
        try:
            logins = search_logins(vault, query, request.app.state.login_names)
        except VaultActionError:
            continue
        if logins:
            return logins, query
    query = (state.name or state.connector).strip()
    return None, query


def _setup_context(
    state: VendorWizardState,
    connectors: list[object],
    form_key: str,
    message: str = "",
) -> dict[str, object]:
    return {
        "state": state,
        "connectors": connectors,
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
) -> dict[str, object]:
    return {
        "state": state,
        "locked": locked,
        "logins": logins,
        "query": query,
        "form_key": form_key,
        "message": message,
    }


def _step(
    request: Request,
    block_name: str,
    status: int = 200,
    **context: object,
) -> HTMLResponse:
    return render_block(request, FRAGMENTS, block_name, status=status, **context)
