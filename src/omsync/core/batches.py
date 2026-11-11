"""Batches: the one way omsync changes data in bulk.

A preview lists the changes an operation would make and the data revision it was computed
from. It writes nothing, so it is safe to show. apply() writes a preview in one transaction
after checking that the data still matches it. undo() reverses an applied batch while nothing
it changed has changed since, and no later batch depends on it. undo_all() reverses several in one
transaction, all or none.

apply() and undo() own the session's transaction. They commit when they succeed and roll back
on any error, so the session must have no unsaved changes when they start.

Both bump the revision as their first write. SQLite admits one writer at a time, so that write
queues concurrent batches, and the checks that follow read data that no other writer can change
until this transaction ends.

Tables are reached by name through Base.metadata, so the models a change touches must be
imported first. This module never imports omsync.ledger or omsync.web.
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,  # noqa: TID251  annotates Core table columns; nothing is mapped here
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Table,
    delete,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch, BatchChange, BatchState, DataRevision
from omsync.core.db import Base

_REVISION: Table = DataRevision.__table__
_RESERVED_TABLES = frozenset({"batch", "batch_change", "data_revision"})


class BatchError(Exception):
    """A batch cannot be previewed, applied, or undone. Nothing was written."""


class StalePreview(BatchError):
    """The data changed after the preview was made, so the preview no longer applies."""


class UndoConflict(BatchError):
    """A batch cannot be undone yet. `rows` lists the (table_name, row_id) pairs in the way."""

    def __init__(self, message: str, rows: Iterable[tuple[str, str]] = ()) -> None:
        super().__init__(message)
        self.rows: list[tuple[str, str]] = list(rows)


@dataclass(frozen=True)
class Change:
    """One row's change. `before` is None for an insert, and `after` is None for a delete.

    Values are JSON-safe: dates and datetimes are ISO strings, and enums are their values.
    `row_id` is the primary key as text. It may be None for an insert whose key is
    autoincrement; apply() records the key it assigned.
    """

    table: str
    row_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None


@dataclass(frozen=True)
class Preview:
    """The changes an operation would make, computed against one data revision."""

    kind: str
    base_revision: int
    changes: tuple[Change, ...]
    summary: dict[str, Any]


def make_preview(
    session: Session,
    kind: str,
    changes: Iterable[Change],
    summary: dict[str, Any] | None = None,
) -> Preview:
    """Record the changes an operation would make, at the current revision. Writes nothing.

    A missing revision row counts as 0, and it is not created here. That keeps a preview a
    pure read, so the session has nothing pending afterward.
    """
    normalized = tuple(_normalize(change) for change in changes)
    return Preview(
        kind=kind,
        base_revision=_read_revision(session),
        changes=normalized,
        summary=_json_value(dict(summary or {})),
    )


def preview_digest(preview: Preview | None) -> str:
    """A token for exactly what a preview would do: its kind, its data revision, and each change.

    A page that shows a preview carries this token, and apply refuses if the operation now differs.
    Counts can match while the transactions, receipt revisions, or allocations differ, so the apply
    binds to the whole operation rather than to a count. Empty when there is no preview.
    """
    if preview is None:
        return ""
    body = [
        preview.kind,
        preview.base_revision,
        [[change.table, change.row_id, change.before, change.after] for change in preview.changes],
    ]
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def snapshot(session: Session, table: str, row_id: str | int) -> dict[str, Any] | None:
    """Return the row as a JSON-safe dict keyed by column name, or None if it does not exist."""
    target = _table(table)
    pk = _primary_key(target)
    return _select_row(session, target, pk, _pk_value(pk, row_id))


def applied_batch_for_key(session: Session, idempotency_key: str) -> Batch | None:
    """Return the applied batch that a key already made, or None.

    Callers that validate before previewing check this first, so a repeated call returns the
    first batch the way apply does, instead of failing on the state that call left behind.
    A key whose batch was undone returns None; apply then refuses it.
    """
    batch = _batch_for_key(session, idempotency_key)
    if batch is not None and batch.state is BatchState.applied:
        return batch
    return None


def next_primary_key(session: Session, table: Table) -> int:
    """Return the first key no row of this table has used, for pre-assigned inserts.

    A table declared with sqlite_autoincrement never reuses a key, even after a delete, so the
    counter SQLite keeps for it is consulted too. Other tables may reuse a key after a delete.
    That is safe because a preview is discarded when anything changes before apply.
    """
    key = _primary_key(table)
    highest = session.execute(select(func.max(key))).scalar_one() or 0
    if table.dialect_options["sqlite"].get("autoincrement"):
        counter = session.execute(
            text("SELECT seq FROM sqlite_sequence WHERE name = :name"), {"name": table.name}
        ).scalar_one_or_none()
        highest = max(highest, counter or 0)
    return highest + 1


def apply(session: Session, preview: Preview, *, idempotency_key: str) -> Batch:
    """Write a preview's changes as one batch, committed in one transaction.

    The same key applies at most once: a repeat call returns the batch the first call made.
    Raises StalePreview if the data changed since the preview was made, and BatchError if a
    change breaks a constraint or names something batches cannot change. On any error,
    nothing is written.
    """
    if not idempotency_key:
        raise BatchError("apply needs a non-empty idempotency key")
    _require_no_unsaved_changes(session)
    existing = _batch_for_key(session, idempotency_key)
    if existing is not None:
        return _applied_or_raise(existing, idempotency_key)
    try:
        batch = _write_batch(session, preview, idempotency_key)
        session.commit()
    except StalePreview:
        session.rollback()
        # On a double-click, the call that committed first wins. This call returns its batch.
        winner = _batch_for_key(session, idempotency_key)
        if winner is not None:
            return _applied_or_raise(winner, idempotency_key)
        raise
    except IntegrityError as exc:
        session.rollback()
        raise BatchError(f"batch could not be applied: {exc.orig}") from exc
    except BaseException:
        session.rollback()
        raise
    return batch


def undo(session: Session, batch_id: int) -> Batch:
    """Reverse an applied batch, if nothing it changed has changed since and no later batch
    depends on it. On any error, nothing is written.

    Raises BatchError if the batch is not applied, for example because it was already undone.
    Raises UndoConflict, naming the rows in the way, if the data has moved on.
    """
    return undo_all(session, [batch_id])[0]


def undo_all(session: Session, batch_ids: Sequence[int]) -> list[Batch]:
    """Reverse several applied batches in the order given, in one transaction.

    Each is checked as undo checks it, after the ones before it were reversed, so a batch that
    later ones depend on is undone after them. If any cannot be undone, none is: nothing is
    written, and the error is the one that batch raised.
    """
    _require_no_unsaved_changes(session)
    try:
        undone = [_undo_one(session, batch_id) for batch_id in batch_ids]
        session.commit()
    except BaseException:
        session.rollback()
        raise
    return undone


def _undo_one(session: Session, batch_id: int) -> Batch:
    _bump_revision(session)
    batch = session.scalars(
        select(Batch).where(Batch.id == batch_id).execution_options(populate_existing=True)
    ).first()
    if batch is None:
        raise BatchError(f"there is no batch {batch_id}")
    if batch.state is not BatchState.applied:
        raise BatchError(f"batch {batch_id} is {batch.state.value}, so it cannot be undone")
    changes = list(
        session.scalars(
            select(BatchChange).where(BatchChange.batch_id == batch_id).order_by(BatchChange.id)
        )
    )
    later = _rows_changed_by_later_batches(session, batch_id, changes)
    if later:
        raise UndoConflict(
            f"batch {batch_id} cannot be undone: a later batch changed {_describe(later)}",
            later,
        )
    moved = _rows_moved_since(session, changes)
    if moved:
        raise UndoConflict(
            f"batch {batch_id} cannot be undone: {_describe(moved)} changed after apply",
            moved,
        )
    for change in reversed(changes):
        _undo_change(session, change)
    batch.state = BatchState.undone
    batch.undone_at = _utc_now()
    return batch


# Revision


def _read_revision(session: Session) -> int:
    value = session.execute(
        select(_REVISION.c.value).where(_REVISION.c.id == 1)
    ).scalar_one_or_none()
    return 0 if value is None else int(value)


def _seed_revision(session: Session) -> None:
    session.execute(
        sqlite_insert(_REVISION).values(id=1, value=0).on_conflict_do_nothing(index_elements=["id"])
    )


def _claim_revision(session: Session, expected: int) -> None:
    """Advance the revision only if it still equals `expected`. This is apply's first write."""
    _seed_revision(session)
    claimed = session.execute(
        update(_REVISION)
        .where(_REVISION.c.id == 1, _REVISION.c.value == expected)
        .values(value=_REVISION.c.value + 1)
    ).rowcount
    if claimed != 1:
        raise StalePreview(
            f"the preview was made at revision {expected}, "
            f"but the data is now at revision {_read_revision(session)}"
        )


def _bump_revision(session: Session) -> None:
    _seed_revision(session)
    session.execute(
        update(_REVISION).where(_REVISION.c.id == 1).values(value=_REVISION.c.value + 1)
    )


# Apply


def _write_batch(session: Session, preview: Preview, idempotency_key: str) -> Batch:
    _claim_revision(session, preview.base_revision)
    batch = Batch(
        kind=preview.kind,
        state=BatchState.applied,
        base_revision=preview.base_revision,
        idempotency_key=idempotency_key,
        summary=dict(preview.summary),
        applied_at=_utc_now(),
    )
    session.add(batch)
    session.flush()
    for raw in preview.changes:
        change = _normalize(raw)
        target = _table(change.table)
        row_id, before_row, after_row = _write_change(session, target, _primary_key(target), change)
        session.add(
            BatchChange(
                batch_id=batch.id,
                table_name=change.table,
                row_id=row_id,
                before=before_row,
                after=after_row,
            )
        )
    return batch


def _write_change(
    session: Session, target: Table, pk: Column, change: Change
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    """Check one change's precondition and write it. Return the row id and both snapshots."""
    try:
        if change.before is None:
            return _insert_row(session, target, pk, change)
        if change.after is None:
            return _delete_row(session, target, pk, change)
        return _update_row(session, target, pk, change)
    except IntegrityError as exc:
        where = f"{target.name} row {change.row_id or '(new)'}"
        raise BatchError(f"{where} breaks a constraint: {exc.orig}") from exc


def _insert_row(
    session: Session, target: Table, pk: Column, change: Change
) -> tuple[str, None, dict[str, Any] | None]:
    if change.row_id is not None and (
        _select_row(session, target, pk, _pk_value(pk, change.row_id)) is not None
    ):
        raise StalePreview(f"{target.name} row {change.row_id} already exists")
    values = _column_values(target, change.after or {})
    if change.row_id is not None:
        values[pk.name] = _pk_value(pk, change.row_id)
    new_key = session.execute(insert(target).values(values)).inserted_primary_key[0]
    if new_key is None:
        raise BatchError(f"{target.name} needs a row id for this insert")
    return str(new_key), None, _select_row(session, target, pk, new_key)


def _delete_row(
    session: Session, target: Table, pk: Column, change: Change
) -> tuple[str, dict[str, Any], None]:
    pk_value = _pk_value(pk, change.row_id)
    current = _select_row(session, target, pk, pk_value)
    if current is None or not _matches(current, change.before or {}):
        raise StalePreview(
            f"{target.name} row {change.row_id} changed or was removed since the preview"
        )
    dependents = _dependent_tables(session, target, current)
    if dependents:
        raise BatchError(
            f"{target.name} row {change.row_id} is still referenced by {', '.join(dependents)}; "
            "delete those rows earlier in this batch"
        )
    session.execute(delete(target).where(pk == pk_value))
    return str(change.row_id), current, None


def _update_row(
    session: Session, target: Table, pk: Column, change: Change
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    pk_value = _pk_value(pk, change.row_id)
    current = _select_row(session, target, pk, pk_value)
    if current is None or not _matches(current, change.before or {}):
        raise StalePreview(
            f"{target.name} row {change.row_id} changed or was removed since the preview"
        )
    changed = {name: value for name, value in (change.after or {}).items() if name != pk.name}
    if changed:
        session.execute(
            update(target).where(pk == pk_value).values(_column_values(target, changed))
        )
    return str(change.row_id), current, _select_row(session, target, pk, pk_value)


# Undo


def _undo_change(session: Session, change: BatchChange) -> None:
    target = _table(change.table_name)
    pk = _primary_key(target)
    row_id = change.row_id
    try:
        if change.before is None:
            # Undo an insert: the row is still as the batch left it, which _rows_moved_since
            # has already checked. It must not have dependents, or they would be cascaded away.
            pk_value = _pk_value(pk, row_id)
            current = _select_row(session, target, pk, pk_value)
            dependents = _dependent_tables(session, target, current) if current else []
            if dependents:
                raise UndoConflict(
                    f"{target.name} row {row_id} is referenced by {', '.join(dependents)}",
                    [(change.table_name, row_id)],
                )
            session.execute(delete(target).where(pk == pk_value))
        elif change.after is None:
            session.execute(insert(target).values(_column_values(target, change.before)))
        else:
            restored = {name: value for name, value in change.before.items() if name != pk.name}
            if restored:
                session.execute(
                    update(target)
                    .where(pk == _pk_value(pk, row_id))
                    .values(_column_values(target, restored))
                )
    except IntegrityError as exc:
        raise UndoConflict(
            f"{target.name} row {row_id} cannot be restored: "
            "rows it depends on, or that depend on it, are in the way",
            [(change.table_name, row_id)],
        ) from exc


def _rows_changed_by_later_batches(
    session: Session, batch_id: int, changes: list[BatchChange]
) -> list[tuple[str, str]]:
    wanted = {(change.table_name, change.row_id) for change in changes}
    if not wanted:
        return []
    rows = session.execute(
        select(BatchChange.table_name, BatchChange.row_id)
        .join(Batch, Batch.id == BatchChange.batch_id)
        .where(
            Batch.id > batch_id,
            Batch.state == BatchState.applied,
            BatchChange.table_name.in_(sorted({table for table, _ in wanted})),
            BatchChange.row_id.in_(sorted({row for _, row in wanted})),
        )
    ).all()
    return sorted({(table, row) for table, row in rows if (table, row) in wanted})


def _rows_moved_since(session: Session, changes: list[BatchChange]) -> list[tuple[str, str]]:
    """Rows whose current state differs from what the batch left. A deleted row is absent,
    which matches an after of None."""
    moved: list[tuple[str, str]] = []
    for change in changes:
        target = _table(change.table_name)
        pk = _primary_key(target)
        if _select_row(session, target, pk, _pk_value(pk, change.row_id)) != change.after:
            moved.append((change.table_name, change.row_id))
    return moved


# Validation and conversion


def _normalize(change: Change) -> Change:
    """Check a change's shape and return it with JSON-safe values and a text row id."""
    target = _table(change.table)
    pk = _primary_key(target)
    before = _json_dict(change.before)
    after = _json_dict(change.after)
    row_id = None if change.row_id is None else str(change.row_id)
    if before is None and after is None:
        raise BatchError(f"change to {target.name} has neither a before nor an after value")
    if before is None and not after:
        raise BatchError(f"an insert into {target.name} needs at least one value")
    if before is not None and row_id is None:
        raise BatchError(f"a change to {target.name} needs the row id it changes")
    for image in (before, after):
        if image is None:
            continue
        unknown = [name for name in image if name not in target.c]
        if unknown:
            raise BatchError(f"{target.name} has no column {unknown[0]!r}")
        if row_id is not None and pk.name in image and str(image[pk.name]) != row_id:
            raise BatchError(f"row id {row_id} does not match {target.name}.{pk.name} in a value")
    return Change(table=change.table, row_id=row_id, before=before, after=after)


def _table(name: str) -> Table:
    if name in _RESERVED_TABLES:
        raise BatchError(f"{name} is batch bookkeeping and cannot be changed by a batch")
    table = Base.metadata.tables.get(name)
    if table is None:
        raise BatchError(f"no table named {name!r}; import its model before using batches")
    return table


def _primary_key(table: Table) -> Column:
    columns = list(table.primary_key.columns)
    if len(columns) != 1:
        kind = "a composite primary key" if columns else "no primary key"
        raise BatchError(f"{table.name} has {kind}; batches need a single-column key")
    return columns[0]


def _pk_value(pk: Column, row_id: str | int) -> Any:
    if not isinstance(row_id, str):
        return row_id
    try:
        return _column_value(pk, row_id)
    except (TypeError, ValueError) as exc:
        raise BatchError(f"{pk.table.name} row id {row_id!r} is not a valid key") from exc


def _select_row(session: Session, table: Table, pk: Column, pk_value: Any) -> dict[str, Any] | None:
    row = session.execute(select(table).where(pk == pk_value)).first()
    if row is None:
        return None
    return {column.name: _json_value(row._mapping[column]) for column in table.columns}


def _matches(current: dict[str, Any], expected: dict[str, Any]) -> bool:
    return all(name in current and current[name] == value for name, value in expected.items())


def _dependent_tables(session: Session, table: Table, row: dict[str, Any]) -> list[str]:
    """Return the tables holding rows that reference this row through a foreign key.

    Checked explicitly because a cascading foreign key would delete the dependent rows
    silently, and a batch records only the changes it was asked to make.
    """
    dependents: list[str] = []
    for child in Base.metadata.tables.values():
        for foreign_key in child.foreign_keys:
            parent = foreign_key.column
            if parent.table is not table or row.get(parent.name) is None:
                continue
            child_column = child.c[foreign_key.parent.name]
            found = session.execute(
                select(child_column)
                .where(child_column == _column_value(parent, row[parent.name]))
                .limit(1)
            ).first()
            if found is not None and child.name not in dependents:
                dependents.append(child.name)
    return dependents


def _column_values(table: Table, values: dict[str, Any]) -> dict[str, Any]:
    converted: dict[str, Any] = {}
    for name, value in values.items():
        try:
            converted[name] = _column_value(table.c[name], value)
        except (KeyError, TypeError, ValueError) as exc:
            raise BatchError(f"{table.name}.{name} cannot hold {value!r}") from exc
    return converted


def _column_value(column: Column, value: Any) -> Any:
    """Turn a JSON-safe value back into what the column stores, by the column's type."""
    if value is None:
        return None
    column_type = column.type
    if isinstance(column_type, DateTime):
        return value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if isinstance(column_type, Date):
        return value if isinstance(value, date) else date.fromisoformat(value)
    if isinstance(column_type, SAEnum) and column_type.enum_class is not None:
        return column_type.enum_class(value)
    if isinstance(column_type, Boolean):
        return bool(value)
    if isinstance(column_type, Integer):
        return int(value)
    if isinstance(column_type, Float):
        return float(value)
    if isinstance(column_type, String):
        return str(value)
    # JSON columns already hold JSON values.
    return value


def _json_value(value: Any) -> Any:
    """Return a JSON-safe copy of a value: dates as ISO strings, enums as their values."""
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    raise BatchError(f"a {type(value).__name__} value cannot be stored in a batch")


def _json_dict(value: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if value is None else _json_value(dict(value))


def _require_no_unsaved_changes(session: Session) -> None:
    if session.new or session.dirty or session.deleted:
        raise BatchError(
            "the session has unsaved changes; commit or roll them back before running a batch"
        )


def _batch_for_key(session: Session, idempotency_key: str) -> Batch | None:
    return session.scalars(
        select(Batch)
        .where(Batch.idempotency_key == idempotency_key)
        .execution_options(populate_existing=True)
    ).first()


def _applied_or_raise(batch: Batch, idempotency_key: str) -> Batch:
    if batch.state is BatchState.applied:
        return batch
    raise BatchError(
        f"idempotency key {idempotency_key!r} belongs to batch {batch.id}, "
        f"which is {batch.state.value}"
    )


def _describe(rows: list[tuple[str, str]]) -> str:
    return ", ".join(f"{table} row {row_id}" for table, row_id in rows)


def _utc_now() -> datetime:
    # Naive UTC, to match the server default that CURRENT_TIMESTAMP gives created_at.
    return datetime.now(UTC).replace(tzinfo=None)
