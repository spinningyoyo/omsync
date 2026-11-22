"""Tags: colored labels for transactions and split lines, and every write to them.

Each write is one batch from omsync.core.batches, so it applies once per idempotency key and can
be undone. Deleting a tag deletes its associations as explicit changes, never through a cascade,
so undo puts them back.
"""

import random
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    Change,
    applied_batch_for_key,
    apply,
    make_preview,
    next_primary_key,
    snapshot,
)
from omsync.core.db import Base
from omsync.ledger.transactions import Split

HEX_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_COLOR = "#737373"
TAG_COLORS: tuple[str, ...] = (
    "#e99537",
    "#4da568",
    "#6471eb",
    "#db5a54",
    "#df4e92",
    "#c44fe9",
    "#eb5429",
    "#61c9ea",
    "#805dee",
    "#6ad28a",
)
NAME_LENGTH = 64


class Tag(Base):
    """A user-defined label with a color that can be applied to transactions and splits."""

    __tablename__ = "tag"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(NAME_LENGTH), unique=True, index=True)
    color: Mapped[str] = mapped_column(
        String(7), default=DEFAULT_COLOR, server_default=DEFAULT_COLOR
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class TransactionTag(Base):
    """Associates a tag with a transaction."""

    __tablename__ = "transaction_tag"
    __table_args__ = (
        UniqueConstraint("transaction_id", "tag_id", name="uq_transaction_tag"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_id: Mapped[int] = mapped_column(
        ForeignKey("transaction.id", ondelete="CASCADE"), index=True
    )
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class SplitTag(Base):
    """Associates a tag with a split."""

    __tablename__ = "split_tag"
    __table_args__ = (
        UniqueConstraint("split_id", "tag_id", name="uq_split_tag"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    split_id: Mapped[int] = mapped_column(ForeignKey("split.id", ondelete="CASCADE"), index=True)
    tag_id: Mapped[int] = mapped_column(ForeignKey("tag.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


TagLink = type[TransactionTag] | type[SplitTag]


def link_owner(link: TagLink) -> Any:
    """The column that names what the tag is on: a transaction or a split line."""
    return link.transaction_id if link is TransactionTag else link.split_id  # type: ignore[union-attr]


def validate_tag_name(name: str) -> str:
    """The name without surrounding space. Raises ValueError if it is empty or too long."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("Tag name cannot be empty.")
    if len(cleaned) > NAME_LENGTH:
        raise ValueError(f"Tag name must be {NAME_LENGTH} characters or fewer.")
    return cleaned


def validate_tag_color(color: str) -> str:
    """The color as lowercase #rrggbb, or the default for none. Raises ValueError for a bad one."""
    cleaned = (color or "").strip()
    if not cleaned:
        return DEFAULT_COLOR
    if not cleaned.startswith("#"):
        cleaned = f"#{cleaned}"
    if not HEX_COLOR_PATTERN.match(cleaned):
        raise ValueError(f"Invalid hex color: {color!r}. Use #rrggbb format.")
    return cleaned.lower()


def random_color() -> str:
    """A color of the palette, for the form of a new tag."""
    return random.choice(TAG_COLORS)  # noqa: S311  a color for display, not a secret


# Reads


def list_tags(session: Session) -> list[Tag]:
    """Every tag, ordered by name without regard to case."""
    return list(session.scalars(select(Tag).order_by(func.lower(Tag.name))).all())


def get_tag(session: Session, tag_id: int) -> Tag | None:
    return session.get(Tag, tag_id)


def get_tag_by_name(session: Session, name: str) -> Tag | None:
    """The tag with this name, without regard to case."""
    return session.scalars(select(Tag).where(func.lower(Tag.name) == name.strip().lower())).first()


def tag_counts(session: Session) -> dict[int, int]:
    """How many transactions carry each tag, on the transaction or on one of its split lines."""
    carriers: dict[int, set[int]] = {}
    for tag_id, txn_id in session.execute(
        select(TransactionTag.tag_id, TransactionTag.transaction_id)
    ):
        carriers.setdefault(tag_id, set()).add(txn_id)
    for tag_id, txn_id in session.execute(
        select(SplitTag.tag_id, Split.transaction_id).join(Split, Split.id == SplitTag.split_id)
    ):
        carriers.setdefault(tag_id, set()).add(txn_id)
    return {tag_id: len(txns) for tag_id, txns in carriers.items()}


# Association changes, shared by every write that puts tags on transactions or split lines


def link_changes(
    session: Session,
    link: TagLink,
    *,
    add: Iterable[tuple[int, int]] = (),
    remove: Iterable[tuple[int, int]] = (),
) -> list[Change]:
    """The changes that put (owner id, tag id) pairs on, and take pairs off. `link` says whether
    the owner is a transaction or a split line. A pair already as wanted writes nothing."""
    owner_column = link_owner(link)
    adds = list(dict.fromkeys(add))
    removes = list(dict.fromkeys(remove))
    owners = {owner for owner, _ in (*adds, *removes)}
    if not owners:
        return []
    existing = {
        (owner, tag_id): row_id
        for row_id, owner, tag_id in session.execute(
            select(link.id, owner_column, link.tag_id).where(owner_column.in_(owners))
        )
    }
    owner_name = owner_column.key
    table = link.__tablename__
    changes = [
        Change(table, str(existing[pair]), snapshot(session, table, existing[pair]), None)
        for pair in removes
        if pair in existing
    ]
    base_id = next_primary_key(session, link.__table__)
    new_pairs = [pair for pair in adds if pair not in existing]
    changes.extend(
        Change(
            table,
            str(base_id + index),
            None,
            {"id": base_id + index, owner_name: owner, "tag_id": tag_id},
        )
        for index, (owner, tag_id) in enumerate(new_pairs)
    )
    return changes


def retag_changes(
    session: Session, link: TagLink, owner_id: int, tag_ids: Iterable[int]
) -> list[Change]:
    """The changes that leave exactly `tag_ids` on one transaction or split line."""
    wanted = set(tag_ids)
    check_tags_exist(session, wanted)
    owner_column = link_owner(link)
    current = set(session.scalars(select(link.tag_id).where(owner_column == owner_id)))
    return link_changes(
        session,
        link,
        add=[(owner_id, tag_id) for tag_id in sorted(wanted - current)],
        remove=[(owner_id, tag_id) for tag_id in sorted(current - wanted)],
    )


def split_tag_deletes(session: Session, split_ids: Sequence[int]) -> list[Change]:
    """Changes that delete the tags of split lines, for a write that deletes the lines."""
    if not split_ids:
        return []
    rows = session.execute(
        select(SplitTag.split_id, SplitTag.tag_id).where(SplitTag.split_id.in_(split_ids))
    ).all()
    return link_changes(session, SplitTag, remove=[(split, tag) for split, tag in rows])


def check_tags_exist(session: Session, tag_ids: Iterable[int]) -> None:
    """Raises ValueError for a tag id that no tag has."""
    wanted = set(tag_ids)
    if not wanted:
        return
    found = set(session.scalars(select(Tag.id).where(Tag.id.in_(wanted))))
    if wanted - found:
        raise ValueError("Pick a tag that exists.")


# Writes


def new_tag_change(session: Session, name: str, color: str) -> tuple[int, str, Change]:
    """The change that inserts a tag, with the id it will have and its cleaned name. Raises
    ValueError for a bad name or color, or a name that a tag already has."""
    clean_name = validate_tag_name(name)
    clean_color = validate_tag_color(color)
    if get_tag_by_name(session, clean_name) is not None:
        raise ValueError(f"Tag {clean_name!r} already exists.")
    new_id = next_primary_key(session, Tag.__table__)
    values = {"id": new_id, "name": clean_name, "color": clean_color}
    return new_id, clean_name, Change("tag", str(new_id), None, values)


def create_tag(session: Session, name: str, color: str = "", *, idempotency_key: str) -> Batch:
    """Add a tag. Names are unique without regard to case. Raises ValueError for a clash."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    new_id, clean_name, change = new_tag_change(session, name, color)
    return _write(
        session,
        "tag.create",
        [change],
        f"Created tag {clean_name}",
        idempotency_key,
        tag_id=new_id,
    )


def update_tag(
    session: Session, tag_id: int, name: str, color: str, *, idempotency_key: str
) -> Batch | None:
    """Rename or recolor a tag. Returns None when it already has these values. Raises
    LookupError for an unknown tag and ValueError for a bad value or a name another tag has."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _tag_snapshot(session, tag_id)
    after = {**before, "name": validate_tag_name(name), "color": validate_tag_color(color)}
    if after == before:
        return None
    clash = get_tag_by_name(session, after["name"])
    if clash is not None and clash.id != tag_id:
        raise ValueError(f"Tag {after['name']!r} already exists.")
    return _write(
        session,
        "tag.update",
        [Change("tag", str(tag_id), before, after)],
        f"Updated tag {after['name']}",
        idempotency_key,
        tag_id=tag_id,
    )


def delete_tag(
    session: Session,
    tag_id: int,
    *,
    move_to: int | None = None,
    idempotency_key: str,
) -> Batch:
    """Delete a tag and take it off every transaction and split line that has it, as one batch
    that undo reverses. With `move_to`, those transactions and split lines get that tag instead;
    one that already has it just loses the deleted tag. Raises LookupError for an unknown tag and
    ValueError when `move_to` is the tag itself or does not exist."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    before = _tag_snapshot(session, tag_id)
    target_name = ""
    if move_to is not None:
        if move_to == tag_id:
            raise ValueError("Pick a different tag to move its transactions to.")
        target = get_tag(session, move_to)
        if target is None:
            raise ValueError("Pick a tag to move its transactions to.")
        target_name = target.name
    changes = [*_use_changes(session, tag_id, move_to), Change("tag", str(tag_id), before, None)]
    name = before["name"]
    label = (
        f"Deleted tag {name} and moved its transactions to {target_name}"
        if move_to is not None
        else f"Deleted tag {name}"
    )
    return _write(session, "tag.delete", changes, label, idempotency_key, tag_id=tag_id)


def delete_all_tags(session: Session, *, idempotency_key: str) -> Batch:
    """Delete every tag and take them off every transaction and split line. Raises ValueError
    when there are no tags."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    tags = list_tags(session)
    if not tags:
        raise ValueError("There are no tags to delete.")
    changes: list[Change] = []
    for tag in tags:
        changes.extend(_use_changes(session, tag.id, None))
    changes.extend(Change("tag", str(tag.id), _tag_snapshot(session, tag.id), None) for tag in tags)
    count = len(tags)
    return _write(
        session,
        "tag.delete_all",
        changes,
        f"Deleted {count} {'tag' if count == 1 else 'tags'}",
        idempotency_key,
        tags=count,
    )


# Helpers


def _use_changes(session: Session, tag_id: int, move_to: int | None) -> list[Change]:
    """Changes that take a tag off everything that has it, or move it to another tag."""
    changes: list[Change] = []
    for link in (TransactionTag, SplitTag):
        owner_column = link_owner(link)
        rows = session.execute(
            select(link.id, owner_column).where(link.tag_id == tag_id).order_by(link.id)
        ).all()
        carrying_target: set[int] = set()
        if move_to is not None and rows:
            carrying_target = set(
                session.scalars(
                    select(owner_column).where(
                        link.tag_id == move_to, owner_column.in_([owner for _, owner in rows])
                    )
                )
            )
        table = link.__tablename__
        for row_id, owner in rows:
            before = snapshot(session, table, row_id)
            if move_to is None or owner in carrying_target:
                changes.append(Change(table, str(row_id), before, None))
            else:
                changes.append(
                    Change(table, str(row_id), before, {**(before or {}), "tag_id": move_to})
                )
    return changes


def _tag_snapshot(session: Session, tag_id: int) -> dict[str, Any]:
    before = snapshot(session, "tag", tag_id)
    if before is None:
        raise LookupError(f"There is no tag {tag_id}.")
    return before


def _write(
    session: Session,
    kind: str,
    changes: list[Change],
    label: str,
    idempotency_key: str,
    **summary: Any,
) -> Batch:
    preview = make_preview(session, kind, changes, summary={"label": label, **summary})
    return apply(session, preview, idempotency_key=idempotency_key)
