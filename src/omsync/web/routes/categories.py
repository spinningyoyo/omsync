"""The Categories page, its table of categories, and the icon picker.

The page also renders the keyword, amount rule, and triage panels, which categories_keywords.py,
categories_rules.py, and categories_triage.py serve on their own after it loads.
"""

from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batches import BatchError
from omsync.ledger import categories_admin as admin
from omsync.ledger.categories_admin import CategoryRow, NothingChanged
from omsync.ledger.models import CategoryType
from omsync.web.categories_replies import block, events, refused, toast
from omsync.web.categories_view import TableView
from omsync.web.deps import get_db
from omsync.web.icons import has_icon, search_icons
from omsync.web.routes.categories_keywords import keyword_context, list_choices, safe_list
from omsync.web.routes.categories_rules import rule_context
from omsync.web.routes.categories_triage import triage_context
from omsync.web.routes.shell import shell_context

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]


def _table_view(
    show_hidden: int = 0,
    category_type: Annotated[str, Query(alias="type")] = "",
    sort: str = "name",
    direction: Annotated[str, Query(alias="dir")] = "asc",
) -> TableView:
    """The table's view, read from the query string of any request the table makes."""
    return TableView.parse(show_hidden, category_type, sort, direction)


View = Annotated[TableView, Depends(_table_view)]
PAGE = "pages/categories.html"
TABS = ("keywords", "rules", "test", "triage", "import")
ICON_PAGE = 120

# Page


@router.get("/categories", response_class=HTMLResponse)
def categories_page(
    request: Request,
    db: DbSession,
    tab: str = "keywords",
    list_key: Annotated[str | None, Query(alias="list")] = None,
) -> HTMLResponse:
    """The whole page. `tab` and `list` open a given tab and keyword list, so a conflict can link to
    its row."""
    context = shell_context(request, db, active="categories", title="Categories")
    return request.app.state.templates.TemplateResponse(
        request,
        PAGE,
        {
            **context,
            **_category_list(db, TableView()),
            "tab": tab if tab in TABS else "keywords",
            "keywords": keyword_context(db, safe_list(list_key)),
            "amount_rules": rule_context(db),
            "payee": "",
            "amount": "",
            "matches": [],
            "amount_rule": None,
            "names": admin.category_names(db),
            "payee_error": "",
            "triage": triage_context(request, db),
            "lists": list_choices(db),
            "new_form": _new_category_form(),
        },
    )


# Categories


@router.get("/categories/list", response_class=HTMLResponse)
def category_list(request: Request, db: DbSession, view: View) -> HTMLResponse:
    return block(request, "frag_category_list", **_category_list(db, view))


@router.post("/categories", response_class=HTMLResponse)
def create_category(
    request: Request,
    db: DbSession,
    name: Annotated[str, Form()] = "",
    category_type: Annotated[str, Form(alias="type")] = CategoryType.unassigned.value,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        batch = admin.create_category(db, name, category_type, idempotency_key=key)
    except (ValueError, BatchError) as exc:
        form = _new_category_form(name=name, type=category_type, error=str(exc))
        return block(request, "frag_new_category", status=refused(exc), form=form)
    return block(
        request,
        "frag_new_category",
        trigger=events(changed=True, close=True),
        form=_new_category_form(),
        **toast(batch),
    )


@router.post("/categories/{category_id:int}", response_class=HTMLResponse)
def update_category(
    request: Request,
    db: DbSession,
    view: View,
    category_id: int,
    name: Annotated[str, Form()] = "",
    category_type: Annotated[str, Form(alias="type")] = CategoryType.unassigned.value,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Save a category's row. The row's cells all post, so one request carries the whole row."""
    key = idempotency_key or uuid4().hex
    try:
        batch = admin.update_category(
            db, category_id, idempotency_key=key, name=name, type=category_type
        )
    except NothingChanged:
        return block(request, "frag_category_list", **_category_list(db, view))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such category") from None
    except (ValueError, BatchError) as exc:
        return block(
            request,
            "frag_category_list",
            status=refused(exc),
            **_category_list(
                db,
                view,
                drafts={category_id: {"name": name, "type": category_type}},
                errors={category_id: str(exc)},
            ),
        )
    return block(
        request,
        "frag_category_list",
        trigger=events(changed=True),
        **_category_list(db, view),
        **toast(batch),
    )


@router.get("/categories/{category_id:int}/icon-picker", response_class=HTMLResponse)
def icon_picker(request: Request, db: DbSession, view: View, category_id: int) -> HTMLResponse:
    """The icon dialog's body for one category: a search box over every Lucide icon and the first
    page of the grid."""
    return block(
        request,
        "frag_icon_picker",
        category=_category_or_404(db, category_id),
        view=view,
        **_icon_results("", 0),
    )


@router.get("/categories/{category_id:int}/icons", response_class=HTMLResponse)
def icon_results(
    request: Request,
    db: DbSession,
    view: View,
    category_id: int,
    q: str = "",
    offset: Annotated[int, Query(ge=0)] = 0,
) -> HTMLResponse:
    """A page of the grid. The first page replaces the results; a later one adds to them."""
    fragment = "frag_icon_results" if offset == 0 else "frag_icon_items"
    return block(
        request,
        fragment,
        category=_category_or_404(db, category_id),
        view=view,
        **_icon_results(q, offset),
    )


@router.post("/categories/{category_id:int}/icon", response_class=HTMLResponse)
def set_category_icon(
    request: Request,
    db: DbSession,
    view: View,
    category_id: int,
    icon: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Choose a category's icon, or restore the default with a blank one."""
    key = idempotency_key or uuid4().hex
    try:
        if icon and not has_icon(icon):
            raise ValueError("Pick an icon from the list.")
        batch = admin.update_category(db, category_id, idempotency_key=key, icon=icon)
    except NothingChanged:
        return block(
            request,
            "frag_category_list",
            trigger=events(close=True),
            **_category_list(db, view),
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="No such category") from None
    except (ValueError, BatchError) as exc:
        return block(
            request,
            "frag_category_list",
            status=refused(exc),
            **_category_list(db, view, message=str(exc)),
        )
    return block(
        request,
        "frag_category_list",
        trigger=events(changed=True, close=True),
        **_category_list(db, view),
        **toast(batch),
    )


@router.post("/categories/bulk", response_class=HTMLResponse)
def bulk_update_categories(
    request: Request,
    db: DbSession,
    view: View,
    ids: Annotated[list[int] | None, Form()] = None,
    action: Annotated[str, Form()] = "update",
    category_type: Annotated[str, Form(alias="type")] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Set the type, or hide or show, the ticked categories, as one batch. A blank type leaves them
    as they are, and an update with nothing to set is refused."""
    key = idempotency_key or uuid4().hex
    chosen = {
        "update": {"category_type": category_type or None},
        "hide": {"hidden": True},
        "show": {"hidden": False},
    }.get(action)
    if chosen is None:
        raise HTTPException(status_code=422, detail="Unknown action")
    try:
        batch = admin.bulk_update_categories(db, ids or [], idempotency_key=key, **chosen)
    except NothingChanged as exc:
        return block(request, "frag_category_list", **_category_list(db, view, message=str(exc)))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such category") from None
    except (ValueError, BatchError) as exc:
        return block(
            request,
            "frag_category_list",
            status=refused(exc),
            **_category_list(db, view, message=str(exc)),
        )
    return block(
        request,
        "frag_category_list",
        trigger=events(changed=True),
        **_category_list(db, view),
        **toast(batch),
    )


@router.post("/categories/{category_id:int}/hidden", response_class=HTMLResponse)
def set_category_hidden(
    request: Request,
    db: DbSession,
    view: View,
    category_id: int,
    hidden: Annotated[str, Form()] = "1",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        batch = admin.hide_category(db, category_id, hidden == "1", idempotency_key=key)
    except NothingChanged:
        return block(request, "frag_category_list", **_category_list(db, view))
    except LookupError:
        raise HTTPException(status_code=404, detail="No such category") from None
    except BatchError as exc:
        return block(
            request,
            "frag_category_list",
            status=409,
            **_category_list(db, view, message=str(exc)),
        )
    return block(
        request,
        "frag_category_list",
        trigger=events(changed=True),
        **_category_list(db, view),
        **toast(batch),
    )


@router.post("/categories/{category_id:int}/delete", response_class=HTMLResponse)
def delete_category(
    request: Request,
    db: DbSession,
    view: View,
    category_id: int,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        batch = admin.delete_category(db, category_id, idempotency_key=key)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such category") from None
    except (ValueError, BatchError) as exc:
        return block(
            request,
            "frag_category_list",
            status=refused(exc),
            **_category_list(db, view, errors={category_id: str(exc)}),
        )
    return block(
        request,
        "frag_category_list",
        trigger=events(changed=True),
        **_category_list(db, view),
        **toast(batch),
    )


# Helpers


def _category_list(
    db: Session,
    view: TableView,
    *,
    drafts: dict[int, dict[str, Any]] | None = None,
    errors: dict[int, str] | None = None,
    message: str = "",
) -> dict[str, Any]:
    """The table's context. A refused row keeps what was typed (drafts) beside its error."""
    return {
        "rows": admin.category_rows(
            db,
            include_hidden=view.show_hidden,
            category_type=view.type,
            sort=view.sort,
            descending=view.descending,
        ),
        "view": view,
        "drafts": drafts or {},
        "errors": errors or {},
        "message": message,
    }


def _icon_results(query: str, offset: int) -> dict[str, Any]:
    names, total = search_icons(query.strip()[:60], offset, ICON_PAGE)
    return {
        "q": query.strip()[:60],
        "names": names,
        "total": total,
        "next_offset": offset + len(names),
    }


def _new_category_form(**values: Any) -> dict[str, Any]:
    form: dict[str, Any] = {
        "name": "",
        "type": CategoryType.unassigned.value,
        "error": "",
        "key": uuid4().hex,
    }
    form.update(values)
    return form


def _category_or_404(db: Session, category_id: int) -> CategoryRow:
    try:
        return admin.get_category(db, category_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such category") from None
