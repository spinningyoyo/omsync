"""The Tags settings page: the list, the new and edit dialog, and delete with a move."""

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import BatchError
from omsync.ledger.tags import (
    Tag,
    create_tag,
    delete_all_tags,
    delete_tag,
    get_tag,
    list_tags,
    random_color,
    tag_counts,
    update_tag,
)
from omsync.web.deps import get_db
from omsync.web.fragments import render_block
from omsync.web.routes.shell import shell_context
from omsync.web.settings_views import SETTINGS_NAV, TagView

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]
PAGE = "settings/tags.html"
FRAGMENTS = "settings/tag_fragments.html"
PATH = "/settings/tags"
CHANGED = {"tags-changed": True, "close-dialogs": True}
PICK_TARGET = "Pick a tag to move its transactions to."
CHANGED_MESSAGE = "This changed elsewhere. Reload the page."


@dataclass
class TagForm:
    """The new and edit form. `tag_id` is None for a new tag."""

    tag_id: int | None
    name: str
    color: str
    key: str
    error: str = ""


def _views(db: Session) -> list[TagView]:
    counts = tag_counts(db)
    return [TagView(tag.id, tag.name, tag.color, counts.get(tag.id, 0)) for tag in list_tags(db)]


def _block(
    request: Request,
    name: str,
    *,
    status: int = 200,
    trigger: dict[str, bool] | None = None,
    **context: Any,
) -> HTMLResponse:
    return render_block(request, FRAGMENTS, name, status=status, trigger=trigger, **context)


def _done(request: Request, batch: Batch | None, *, status: int = 200) -> HTMLResponse:
    """The reply to a write: the toast that names its batch, and the events that reload the list
    and close the dialog. A write that changed nothing has no toast."""
    label = str((batch.summary or {}).get("label", "Saved")) if batch else None
    return _block(
        request,
        "frag_done",
        status=status,
        trigger=CHANGED,
        label=label,
        batch_id=batch.id if batch else None,
    )


def _refused(request: Request, message: str, status: int = 422) -> HTMLResponse:
    return _block(request, "frag_done", status=status, label=message, batch_id=None)


def _delete_dialog(
    request: Request, db: Session, tag: Tag, key: str, error: str, status: int = 200
) -> HTMLResponse:
    return _block(
        request,
        "frag_delete",
        status=status,
        tag=tag,
        count=tag_counts(db).get(tag.id, 0),
        others=[other for other in list_tags(db) if other.id != tag.id],
        key=key,
        error=error,
    )


def _delete_refused(
    request: Request, db: Session, tag: Tag, key: str, message: str, status: int
) -> HTMLResponse:
    """A refused delete: the dialog again with the reason, or a toast when it came from the list."""
    if request.headers.get("hx-target") == "tag-dialog-body":
        return _delete_dialog(request, db, tag, key, message, status)
    return _refused(request, message, status)


def _tag_or_404(db: Session, tag_id: int) -> Tag:
    tag = get_tag(db, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="No such tag")
    return tag


@router.get(PATH, response_class=HTMLResponse)
def get_tags(request: Request, db: DbSession) -> HTMLResponse:
    context = shell_context(request, db, active="", title="Tags")
    context.update(nav=SETTINGS_NAV, active_href=PATH, tags=_views(db))
    return request.app.state.templates.TemplateResponse(request, PAGE, context)


@router.get(f"{PATH}/list", response_class=HTMLResponse)
def get_list(request: Request, db: DbSession) -> HTMLResponse:
    return _block(request, "frag_list", tags=_views(db))


@router.get(f"{PATH}/new", response_class=HTMLResponse)
def new_form(request: Request) -> HTMLResponse:
    form = TagForm(None, "", random_color(), uuid4().hex)
    return _block(request, "frag_form", form=form)


@router.post(PATH, response_class=HTMLResponse)
def post_create(
    request: Request,
    db: DbSession,
    name: Annotated[str, Form()] = "",
    color: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        batch = create_tag(db, name, color, idempotency_key=key)
    except (ValueError, BatchError) as exc:
        form = TagForm(None, name, color, key, str(exc))
        return _block(request, "frag_form", status=422, form=form)
    return _done(request, batch)


@router.get(PATH + "/{tag_id:int}/edit", response_class=HTMLResponse)
def edit_form(request: Request, db: DbSession, tag_id: int) -> HTMLResponse:
    tag = _tag_or_404(db, tag_id)
    return _block(request, "frag_form", form=TagForm(tag.id, tag.name, tag.color, uuid4().hex))


@router.post(PATH + "/{tag_id:int}", response_class=HTMLResponse)
def post_update(
    request: Request,
    db: DbSession,
    tag_id: int,
    name: Annotated[str, Form()] = "",
    color: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    try:
        batch = update_tag(db, tag_id, name, color, idempotency_key=key)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such tag") from None
    except (ValueError, BatchError) as exc:
        form = TagForm(tag_id, name, color, key, str(exc))
        return _block(request, "frag_form", status=422, form=form)
    return _done(request, batch)


@router.get(PATH + "/{tag_id:int}/delete", response_class=HTMLResponse)
def delete_form(request: Request, db: DbSession, tag_id: int) -> HTMLResponse:
    """The dialog for a tag that transactions carry: move them to another tag, or just remove it."""
    return _delete_dialog(request, db, _tag_or_404(db, tag_id), uuid4().hex, "")


@router.post(PATH + "/{tag_id:int}/delete", response_class=HTMLResponse)
def post_delete(
    request: Request,
    db: DbSession,
    tag_id: int,
    replacement_tag_id: Annotated[str, Form()] = "",
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    key = idempotency_key or uuid4().hex
    tag = _tag_or_404(db, tag_id)
    try:
        move_to = int(replacement_tag_id) if replacement_tag_id else None
    except ValueError:
        return _delete_refused(request, db, tag, key, PICK_TARGET, 422)
    try:
        batch = delete_tag(db, tag_id, move_to=move_to, idempotency_key=key)
    except ValueError as exc:
        return _delete_refused(request, db, tag, key, str(exc), 422)
    except BatchError:
        return _delete_refused(request, db, tag, key, CHANGED_MESSAGE, 409)
    return _done(request, batch)


@router.post(PATH + "/delete-all", response_class=HTMLResponse)
def post_delete_all(
    request: Request,
    db: DbSession,
    idempotency_key: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        batch = delete_all_tags(db, idempotency_key=idempotency_key or uuid4().hex)
    except ValueError as exc:
        return _refused(request, str(exc))
    except BatchError:
        return _refused(request, CHANGED_MESSAGE, 409)
    return _done(request, batch)
