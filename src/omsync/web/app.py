"""FastAPI application factory."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2_fragments.fastapi import Jinja2Blocks
from nursery.assets import FAVICON_PNG as NURSERY_FAVICON_PNG
from nursery.assets import FAVICON_SVG as NURSERY_FAVICON_SVG
from nursery.assets import JS as NURSERY_JS
from nursery.assets import SCENES_CSS as NURSERY_SCENES_CSS

import omsync.models  # noqa: F401 -- every table on Base.metadata
from omsync import __version__
from omsync.core.db import database_url, make_engine, make_session_factory
from omsync.core.money import format_cents
from omsync.core.owner import OwnerCheck, system_owner_check
from omsync.core.sessions import LoginRateLimiter, SessionStore
from omsync.core.settings import Settings, load_settings
from omsync.core.vault import Vault
from omsync.core.vaults import open_vault
from omsync.demo.site import DemoSite
from omsync.ledger.categories import TYPE_LABELS
from omsync.ledger.categorize import list_name
from omsync.ledger.payment_methods import METHOD_LABELS
from omsync.ledger.register import field_version
from omsync.ledger.sources import account_digits
from omsync.ledger.tags import TAG_COLORS, random_color
from omsync.web.amounts import money_html, sensitive
from omsync.web.auth import install_guard
from omsync.web.brand import mascot_svg
from omsync.web.categories_view import TYPE_OPTIONS
from omsync.web.errors import install_error_pages
from omsync.web.filter_forms import FIELD_LABELS, field_choices, op_choices
from omsync.web.formats import long_day
from omsync.web.fragments import undo_url
from omsync.web.icons import category_icon_name, icon_svg
from omsync.web.institution_logos import institution_logo_url
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
    counterparty,
    guides,
    home,
    plan,
    plan_goals,
    receipts,
    reconcile,
    recurring,
    register_bulk,
    register_filters,
    register_review,
    reports,
    shell,
    sign_in,
    transactions,
    undo,
)
from omsync.web.routes.demo import router as demo_router
from omsync.web.routes.settings import router as settings_router
from omsync.web.routes.settings_providers import router as settings_providers_router
from omsync.web.routes.settings_rental import router as settings_rental_router
from omsync.web.routes.settings_tags import router as settings_tags_router
from omsync.web.routes.settings_vault import router as settings_vault_router
from omsync.web.routes.settings_vendor_wizard import router as settings_vendor_wizard_router
from omsync.web.routes.settings_wizard import router as settings_wizard_router
from omsync.web.row_links import link_items
from omsync.web.scenes import nursery_scenes
from omsync.web.settings_tokens import MappingTokens
from omsync.web.sync_runner import SyncRunner
from omsync.web.vault_logins import LoginNames

IDLE_CHECK_SECONDS = 60
logger = logging.getLogger(__name__)


async def _lock_idle_vault(app: FastAPI) -> None:
    # The vault is read on each pass, because connect and disconnect replace it at runtime.
    while True:
        await asyncio.sleep(IDLE_CHECK_SECONDS)
        vault = getattr(app.state, "vault", None)
        if vault is None:
            continue
        try:
            await asyncio.to_thread(vault.expire)
        except Exception:  # a failed lock must not stop later checks
            logger.exception("The idle vault lock failed")


WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


class DynamicHasVault:
    """Evaluates whether a vault is currently configured on the app at template evaluation time.

    Jinja macro imports capture module globals at initial compilation time. Using a dynamic object
    allows templates and cached macros to reflect runtime connect/disconnect of Bitwarden without
    relying on static globals.
    """

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    def __bool__(self) -> bool:
        return self._app.state.vault is not None

    def __repr__(self) -> str:
        return repr(bool(self))

    def __str__(self) -> str:
        return str(bool(self))


def create_app(
    settings: Settings | None = None,
    *,
    vault: Vault | None = None,
    owner_check: OwnerCheck | None = None,
    sessions: SessionStore | None = None,
    demo: DemoSite | None = None,
    vault_opener: Callable[..., Vault | None] | None = None,
) -> FastAPI:
    """Build the app. Without explicit settings, they are loaded from the environment and TOML.

    `vault`, `owner_check` and `sessions` can be injected for tests. Otherwise the vault is the one
    the vault setting names (none by default), sign-in uses the computer's owner check, and
    sessions go in a new in-memory store. Building the app never opens the database, so tests can
    point it at a temporary data directory.

    `demo` turns on demo mode: the demo sign-in, the Reset demo action, and the Demo pill. Pass it
    with DemoVault, which keeps any real vault out of the demo.
    """
    if settings is None:
        settings = load_settings()
    engine = make_engine(database_url(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        idle_lock = asyncio.create_task(_lock_idle_vault(app))
        yield
        idle_lock.cancel()
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
        vault = open_vault(settings)
    app.state.vault = vault
    app.state.vault_opener = vault_opener or open_vault
    app.state.owner_check = owner_check if owner_check is not None else system_owner_check()
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
    templates.env.filters["long_day"] = long_day
    templates.env.globals["icon_svg"] = icon_svg
    templates.env.globals["link_items"] = link_items
    templates.env.globals["undo_url"] = undo_url
    templates.env.globals["category_icon_name"] = category_icon_name
    templates.env.globals["institution_logo_url"] = institution_logo_url
    templates.env.globals["account_digits"] = account_digits
    templates.env.globals["mascot_svg"] = mascot_svg
    templates.env.globals["nursery_scenes"] = nursery_scenes()
    templates.env.globals["filter_field_choices"] = field_choices()
    templates.env.globals["filter_labels"] = FIELD_LABELS
    templates.env.globals["filter_op_choices"] = op_choices
    templates.env.globals["category_type_options"] = TYPE_OPTIONS
    templates.env.globals["list_name"] = list_name
    templates.env.globals["payment_methods"] = [
        (method.value, label) for method, label in METHOD_LABELS.items()
    ]
    templates.env.globals["category_type_labels"] = TYPE_LABELS
    templates.env.globals["field_version"] = field_version
    templates.env.globals["register_page_sizes"] = PAGE_SIZES
    templates.env.globals["new_key"] = lambda: uuid4().hex
    templates.env.globals["tag_colors"] = TAG_COLORS
    templates.env.globals["new_tag_color"] = random_color
    templates.env.globals["demo_mode"] = demo is not None
    templates.env.globals["has_vault"] = DynamicHasVault(app)
    app.state.templates = templates

    install_guard(app)
    install_error_pages(app)
    # Pages, styles and scripts are text, so compressing them makes the first visit on a slow link
    # several times quicker. Starlette leaves event streams uncompressed.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    # The sign-in scenes come from the installed omsync-nursery package; mounted first, so this
    # path does not fall through to the app's own static files. The scenes' colours (css/) sit
    # beside the engine's folder in the package, so they get their own mount, ahead of the
    # engine's.
    # The favicons come from the same package; these two routes sit ahead of the /static mount.
    app.add_api_route(
        "/static/img/favicon.svg",
        lambda: FileResponse(NURSERY_FAVICON_SVG, media_type="image/svg+xml"),
        include_in_schema=False,
    )
    app.add_api_route(
        "/static/img/favicon-32.png",
        lambda: FileResponse(NURSERY_FAVICON_PNG, media_type="image/png"),
        include_in_schema=False,
    )
    app.mount(
        "/static/vendor/nursery/css",
        StaticFiles(directory=NURSERY_SCENES_CSS.parent),
        name="nursery-css",
    )
    app.mount("/static/vendor/nursery", StaticFiles(directory=NURSERY_JS), name="nursery")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(sign_in.router)
    app.include_router(shell.router)
    app.include_router(home.router)
    app.include_router(reports.router)
    app.include_router(transactions.router)
    app.include_router(undo.router)
    app.include_router(accounts.router)
    app.include_router(register_filters.router)
    app.include_router(register_bulk.router)
    app.include_router(register_review.router)
    app.include_router(categories.router)
    app.include_router(categories_keywords.router)
    app.include_router(categories_rules.router)
    app.include_router(categories_triage.router)
    app.include_router(reconcile.router)
    app.include_router(receipts.router)
    app.include_router(recurring.router)
    app.include_router(plan.router)
    app.include_router(plan_goals.router)
    app.include_router(contacts.router)
    app.include_router(contacts_detail.router)
    app.include_router(counterparty.router)
    app.include_router(settings_router)
    app.include_router(settings_providers_router)
    app.include_router(settings_vault_router)
    app.include_router(settings_wizard_router)
    app.include_router(settings_vendor_wizard_router)
    app.include_router(settings_rental_router)
    app.include_router(settings_tags_router)
    app.include_router(guides.router)
    app.include_router(demo_router)

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
