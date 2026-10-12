"""Route test for the bulk edit: the apply as one batch with undo."""

import html
import re

from fastapi import FastAPI
from fastapi.testclient import TestClient
from register_support import (
    count_batches,
    events,
    f_param,
    revision_in,
    seed,
    signed_in_client,
    stored_transaction,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch, BatchState
from omsync.ledger.filters import Condition
from omsync.ledger.models import CategoryOrigin


def preview(client: TestClient, txn_ids: list[int], value: str, **extra: str) -> str:
    """The confirm step for a change. A category's value is its id, as the dialog's list sends."""
    data: dict[str, object] = {"bulk_action": "set_category", "value": value, "ids": txn_ids}
    data.update(extra)
    response = client.post("/transactions/bulk/preview", data=data)
    assert response.status_code == 200, response.text
    return response.text


def test_apply_writes_one_batch_and_undo_restores_every_row(
    client: TestClient, app: FastAPI
) -> None:
    ids = seed(app)
    signed_in_client(client)
    counted = preview(client, [ids["maple"], ids["city"]], str(ids["groceries"]))

    applied = client.post(
        "/transactions/bulk/apply",
        data={
            "bulk_action": "set_category",
            "value": str(ids["groceries"]),
            "ids": [ids["maple"], ids["city"]],
            "f": f_param(Condition("payee", "contains", "maple")),
            "revision": revision_in(counted),
            "idempotency_key": "apply-1",
        },
    )

    assert applied.status_code == 200
    assert events(applied.headers)["bulk-done"] is True
    assert "Set category of 2 transactions to Groceries" in applied.text
    assert 'id="register-table" hx-swap-oob="innerHTML"' in applied.text
    assert count_batches(app, "bulk.category") == 1
    assert stored_transaction(app, ids["maple"]).category_origin is CategoryOrigin.manual

    undo_url = re.search(r'hx-post="(/batches/\d+/undo[^"]*)"', applied.text)
    assert undo_url, "the toast has no Undo button"
    undone = client.post(html.unescape(undo_url.group(1)), data={})

    assert undone.status_code == 200
    assert "Undone" in undone.text
    assert "Maple Grocery" in undone.text  # the register comes back with the filter it was under
    assert "City Power Co" not in undone.text
    assert stored_transaction(app, ids["maple"]).category_id is None
    assert stored_transaction(app, ids["city"]).category_id is None
    with Session(app.state.engine) as db:
        assert (
            db.scalar(select(Batch.state).where(Batch.kind == "bulk.category")) is BatchState.undone
        )
