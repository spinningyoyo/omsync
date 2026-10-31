"""FastAPI application factory."""

import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2_fragments.fastapi import Jinja2Blocks

from omsync import __version__
from omsync.core.db import database_url, make_engine, make_session_factory
from omsync.core.money import format_cents
from omsync.core.sessions import LoginRateLimiter, SessionStore
from omsync.core.settings import Settings, load_settings
from omsync.core.vault import BitwardenCLI, SystemKeychain, Vault
from omsync.demo.site import DemoSite
from omsync.demo.vault import DemoVault
from omsync.ledger.categories import TYPE_LABELS
from omsync.ledger.register import field_version
from omsync.ledger.sources import account_digits
from omsync.web.amounts import money_html, sensitive
from omsync.web.auth import install_guard
from omsync.web.brand import mascot_svg
from omsync.web.categories_view import TYPE_OPTIONS
from omsync.web.errors import install_error_pages
from omsync.web.filter_forms import FIELD_LABELS, field_choices, op_choices
from omsync.web.icons import category_icon_name, icon_svg
from omsync.web.institution_logos import institution_logo_url
from omsync.web.prototype.register_store import RegisterStore, generate_transactions
from omsync.web.register_replies import FRAGMENTS, RegisterError
from omsync.web.register_state import PAGE_SIZES
from omsync.web.routes import (
    accounts,
    categories,
    categories_keywords,
    categories_rules,
    categories_triage,
    contacts,
    contacts_detail,
    dev,
    guides,
    home,
    other_side,
    plan,
    plan_goals,
    prototype_register,
    reconcile,
    recurring,
    register_bulk,
    register_filters,
    register_review,
    reports,
    shell,
    sign_in,
    transactions,
)
from omsync.web.routes.demo import router as demo_router
from omsync.web.routes.settings import router as settings_router
from omsync.web.routes.settings_providers import router as settings_providers_router
from omsync.web.routes.settings_rental import router as settings_rental_router
from omsync.web.routes.settings_wizard import router as settings_wizard_router
from omsync.web.settings_tokens import MappingTokens
from omsync.web.sync_runner import SyncRunner
from omsync.web.vault_logins import LoginNames

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def create_app(
    settings: Settings | None = None,
    *,
    vault: Vault | DemoVault | None = None,
    sessions: SessionStore | None = None,
    demo: DemoSite | None = None,
) -> FastAPI:
    """Build the app. Without explicit settings, they are loaded from the environment and TOML.

    `vault` and `sessions` can be injected for tests. Otherwise the vault wraps the real `bw`
    CLI and the keychain, and sessions go in a new in-memory store. Building the app never opens
    the database, so tests can point it at a temporary data directory.

    `demo` turns on demo mode: the demo sign-in, the Reset demo action, and the Demo pill. Pass it
    with DemoVault, which keeps the Bitwarden vault out of the demo.
    """
    if settings is None:
        settings = load_settings()
    engine = make_engine(database_url(settings))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    # No interactive API docs: their pages load scripts from a public CDN, and nothing may leave
    # this Mac.
    app = FastAPI(
        title="omsync", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)
    if vault is None:
        vault = Vault(
            BitwardenCLI(appdata_dir=settings.data_dir / "bitwarden"),
            SystemKeychain(),
            settings.vault_idle_minutes,
        )
    app.state.vault = vault
    app.state.demo = demo
    app.state.sync_runner = SyncRunner(app.state.session_factory, vault, settings)
    app.state.sessions = sessions if sessions is not None else SessionStore()
    app.state.login_limiter = LoginRateLimiter()
    app.state.login_names = LoginNames()
    app.state.mapping_tokens = MappingTokens()

    templates = Jinja2Blocks(directory=str(TEMPLATES_DIR))
    templates.env.filters["money"] = money_html
    templates.env.filters["sensitive"] = sensitive
    templates.env.filters["money_text"] = format_cents
    templates.env.globals["icon_svg"] = icon_svg
    templates.env.globals["category_icon_name"] = category_icon_name
    templates.env.globals["institution_logo_url"] = institution_logo_url
    templates.env.globals["account_digits"] = account_digits
    templates.env.globals["mascot_svg"] = mascot_svg
    templates.env.globals["filter_field_choices"] = field_choices()
    templates.env.globals["filter_labels"] = FIELD_LABELS
    templates.env.globals["filter_op_choices"] = op_choices
    templates.env.globals["category_type_options"] = TYPE_OPTIONS
    templates.env.globals["category_type_labels"] = TYPE_LABELS
    templates.env.globals["field_version"] = field_version
    templates.env.globals["register_page_sizes"] = PAGE_SIZES
    templates.env.globals["new_key"] = lambda: uuid4().hex
    templates.env.globals["demo_mode"] = demo is not None
    app.state.templates = templates

    # Prototype data only: fake transactions, held in memory and never written to disk.
    app.state.register_store = RegisterStore(generate_transactions(random.Random(42)))

    install_guard(app)
    install_error_pages(app)
    # Pages, styles and scripts are text, so compressing them makes the first visit on a slow link
    # several times quicker. Starlette leaves event streams uncompressed.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(sign_in.router)
    app.include_router(shell.router)
    app.include_router(home.router)
    app.include_router(reports.router)
    app.include_router(transactions.router)
    app.include_router(accounts.router)
    app.include_router(register_filters.router)
    app.include_router(register_bulk.router)
    app.include_router(register_review.router)
    app.include_router(categories.router)
    app.include_router(categories_keywords.router)
    app.include_router(categories_rules.router)
    app.include_router(categories_triage.router)
    app.include_router(reconcile.router)
    app.include_router(recurring.router)
    app.include_router(plan.router)
    app.include_router(plan_goals.router)
    app.include_router(contacts.router)
    app.include_router(contacts_detail.router)
    app.include_router(other_side.router)
    app.include_router(settings_router)
    app.include_router(settings_providers_router)
    app.include_router(settings_wizard_router)
    app.include_router(settings_rental_router)
    app.include_router(guides.router)
    app.include_router(prototype_register.router)
    app.include_router(demo_router)
    app.include_router(dev.router)

    @app.exception_handler(RegisterError)
    def register_error(request: Request, exc: RegisterError) -> HTMLResponse:
        """Answer a register request that cannot be done with its message, as a fragment that htmx
        swaps where the request was aimed."""
        return request.app.state.templates.TemplateResponse(
            request,
            FRAGMENTS,
            {"message": exc.message},
            status_code=exc.status_code,
            block_name="frag_message",
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
