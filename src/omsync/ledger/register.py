"""The transaction register: listing transactions, and editing them as batches.

Each edit is one batch from omsync.core.batches, so it previews, applies once per idempotency
key, and can be undone while nothing it changed has changed since. The web layer calls these
functions and never writes the tables itself. This module never imports omsync.web.

A split transaction has no category of its own. Splitting clears the transaction's category, and
set_category refuses a split transaction. Clearing the splits leaves the transaction unsplit and
uncategorized, so categorize may set it again. check_split_drafts and check_split_lines own the
rules for a split's lines, for the editor's lines and for set_splits alike.

Batch summaries carry "label" (for the undo toast), "transaction_id", and "field" (the cell that
changed, so the web layer can focus it after an undo).

This module also reads the Needs review queue: open import reviews, with the transactions each one
might be the same as. Resolving a review is import.resolve_review, which the web layer calls.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    Change,
    applied_batch_for_key,
    apply,
    make_preview,
    next_primary_key,
    snapshot,
)
from omsync.core.money import format_cents, to_cents
from omsync.ledger.categories import Category
from omsync.ledger.filters import FilterSpec
from omsync.ledger.filters import apply as apply_filter
from omsync.ledger.links import Link, LinkKind
from omsync.ledger.sources import Account, account_digits
from omsync.ledger.statements import StatementFile, StatementRow
from omsync.ledger.tags import (
    SplitTag,
    Tag,
    TagLink,
    TransactionTag,
    check_tags_exist,
    link_changes,
    link_owner,
    new_tag_change,
    retag_changes,
    split_tag_deletes,
)
from omsync.ledger.transactions import (
    CategoryOrigin,
    ContactOrigin,
    ImportReview,
    ReviewStatus,
    Split,
    Transaction,
)

SplitLine = tuple[int, int | None, str | None]  # amount_cents, category_id, memo


@dataclass(frozen=True)
class TagRow:
    """One tag of a transaction or split, as the register shows it."""

    id: int
    name: str
    color: str


@dataclass(frozen=True)
class SplitRow:
    """One split line of a transaction, as the register shows it."""

    id: int
    amount_cents: int
    category_id: int | None
    category: str | None
    memo: str | None
    tags: list[TagRow] = field(default_factory=list)


@dataclass(frozen=True)
class LinkedTransaction:
    """The other transaction of a link, as the register names it."""

    id: int
    posted_on: date
    account_id: int
    account_name: str
    account_digits: str
    amount_cents: int


@dataclass(frozen=True)
class RowLink:
    """What a link ties a transaction to: another transaction, or a receipt. label says what kind
    of link it is, in the words of the rule that made it."""

    kind: LinkKind
    label: str
    other: LinkedTransaction | None = None
    receipt_key: str | None = None


LINK_LABELS = {
    LinkKind.transfer: "Transfer",
    LinkKind.receipt: "Receipt",
    LinkKind.refund: "Refund",
    LinkKind.p2p: "Money sent and sent back",
    LinkKind.joint_settlement: "Joint card bill",
}


@dataclass(frozen=True)
class RegisterRow:
    """One transaction as the register shows it. Splits are in position order."""

    id: int
    posted_on: date
    account_id: int
    account_name: str
    account_digits: str  # see account_digits; empty when the account has no last four
    payee: str
    category_id: int | None
    category: str | None
    category_origin: CategoryOrigin
    amount_cents: int
    memo: str | None
    splits: list[SplitRow] = field(default_factory=list)
    links: list[RowLink] = field(default_factory=list)
    tags: list[TagRow] = field(default_factory=list)

    @property
    def is_split(self) -> bool:
        return bool(self.splits)


# Listing


def list_transactions(
    session: Session,
    *,
    account_id: int | None = None,
    spec: FilterSpec | None = None,
    offset: int = 0,
    limit: int = 100,
) -> list[RegisterRow]:
    """Return up to `limit` transactions after the first `offset`, ordered by (posted_on, id)
    descending, so the newest come first and every transaction has one place in the order.

    `account_id` and `spec` (see omsync.ledger.filters) both narrow the result.
    """
    if limit < 1:
        raise ValueError("limit must be at least 1")
    if offset < 0:
        raise ValueError("offset must not be negative")
    query = _register_query()
    if account_id is not None:
        query = query.where(Transaction.account_id == account_id)
    if spec is not None:
        query = apply_filter(query, spec)
    query = (
        query.order_by(Transaction.posted_on.desc(), Transaction.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return _rows(session, session.execute(query).all())


def get_transaction_row(session: Session, txn_id: int) -> RegisterRow:
    """Return one transaction as the register shows it. Raises LookupError if it does not exist."""
    found = session.execute(_register_query().where(Transaction.id == txn_id)).all()
    if not found:
        raise LookupError(f"there is no transaction {txn_id}")
    return _rows(session, found)[0]


def row_version(row: RegisterRow) -> str:
    """A token for everything the register shows about one transaction and its splits.

    An editor carries the token it was opened with. A save that carries an older token was made
    from an older view, so the web layer refuses it rather than writing over a newer decision. The
    batch engine's revision check cannot catch this: it only sees changes after the preview.
    """
    state = [
        row.posted_on.isoformat(),
        row.amount_cents,
        row.payee,
        row.category_id,
        row.category_origin.value,
        row.memo,
        [
            [split.id, split.amount_cents, split.category, split.memo, [t.id for t in split.tags]]
            for split in row.splits
        ],
        [t.id for t in row.tags],
    ]
    return hashlib.sha256(json.dumps(state).encode()).hexdigest()[:20]


def field_version(row: RegisterRow, field_name: str) -> str:
    """A token for what the register shows in one editable field. A cell save carries the token it
    was rendered with, so a change to that field made elsewhere refuses the save. Changes to the
    other fields do not, so each cell in a row saves on its own."""
    state = {
        "category": [row.category_id, row.category_origin.value, row.is_split],
    }[field_name]
    return hashlib.sha256(json.dumps([field_name, state]).encode()).hexdigest()[:20]


def categories(session: Session) -> list[tuple[int, str]]:
    """Return (id, name) for every category that is not hidden, sorted by name."""
    rows = session.execute(
        select(Category.id, Category.name).where(Category.hidden.is_(False))
    ).all()
    return sorted(
        ((int(category_id), name) for category_id, name in rows), key=lambda r: r[1].casefold()
    )


# Needs review


@dataclass(frozen=True)
class CandidateRow:
    """A transaction an import review might be the same as."""

    id: int
    posted_on: date
    amount_cents: int
    payee: str


@dataclass(frozen=True)
class ReviewRow:
    """One open import review: the statement row, why it is open, and its candidates."""

    id: int
    reason: str
    posted_on: date
    amount_cents: int
    payee_raw: str
    file_name: str
    account_name: str | None
    candidates: tuple[CandidateRow, ...]


def open_reviews(session: Session) -> list[ReviewRow]:
    """Every open import review, newest statement row first."""
    found = session.execute(
        select(ImportReview, StatementRow, StatementFile, Account)
        .select_from(ImportReview)
        .join(StatementRow, StatementRow.id == ImportReview.statement_row_id)
        .join(StatementFile, StatementFile.id == StatementRow.statement_file_id)
        .outerjoin(Account, Account.id == StatementFile.account_id)
        .where(ImportReview.status == ReviewStatus.open)
        .order_by(StatementRow.posted_on.desc(), ImportReview.id)
    ).all()
    candidate_ids = {cid for review, _, _, _ in found for cid in review.candidate_transaction_ids}
    by_id: dict[int, Transaction] = {}
    if candidate_ids:
        by_id = {
            txn.id: txn
            for txn in session.scalars(
                select(Transaction).where(Transaction.id.in_(sorted(candidate_ids)))
            )
        }
    return [
        ReviewRow(
            id=review.id,
            reason=review.reason,
            posted_on=row.posted_on,
            amount_cents=row.amount_cents,
            payee_raw=row.payee_raw,
            file_name=statement_file.original_name,
            account_name=None if account is None else account.name,
            candidates=tuple(
                CandidateRow(txn.id, txn.posted_on, txn.amount_cents, txn.payee)
                for txn_id in review.candidate_transaction_ids
                if (txn := by_id.get(txn_id)) is not None
            ),
        )
        for review, row, statement_file, account in found
    ]


def open_review_count(session: Session) -> int:
    """How many import reviews are open. The Transactions badge and tab show this."""
    return int(
        session.scalar(
            select(func.count(ImportReview.id)).where(ImportReview.status == ReviewStatus.open)
        )
        or 0
    )


# Edits. Each returns the batch it applied. A key that already applied returns that batch.


def edit_memo(session: Session, txn_id: int, memo: str | None, *, idempotency_key: str) -> Batch:
    """Set the memo. An empty memo is stored as None."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    current = _transaction_values(session, txn_id)
    text = (memo or "").strip() or None
    if text:
        label = f"Set memo of {current['payee']} to {text}"
    else:
        label = f"Clear memo of {current['payee']}"
    return _write(
        session,
        kind="register.memo",
        txn_id=txn_id,
        field="memo",
        label=label,
        changes=_field_changes(txn_id, {"memo": current["memo"]}, {"memo": text}),
        idempotency_key=idempotency_key,
    )


def toggle_transaction_tag(
    session: Session, txn_id: int, tag_id: int, *, idempotency_key: str
) -> Batch | None:
    """Put a tag on a transaction, or take it off if it has it. Returns None for a tag that no
    longer exists, so a stale picker changes nothing."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    current = _transaction_values(session, txn_id)
    tag_name = session.scalar(select(Tag.name).where(Tag.id == tag_id))
    if tag_name is None:
        return None
    has = session.scalar(
        select(TransactionTag.id).where(
            TransactionTag.transaction_id == txn_id, TransactionTag.tag_id == tag_id
        )
    )
    pair = [(txn_id, tag_id)]
    if has is None:
        changes = link_changes(session, TransactionTag, add=pair)
        label = f"Added tag {tag_name} to {current['payee']}"
    else:
        changes = link_changes(session, TransactionTag, remove=pair)
        label = f"Removed tag {tag_name} from {current['payee']}"
    return _write(
        session,
        kind="register.tags",
        txn_id=txn_id,
        field="tags",
        label=label,
        changes=changes,
        idempotency_key=idempotency_key,
    )


def create_tag_on_transaction(
    session: Session, txn_id: int, name: str, color: str, *, idempotency_key: str
) -> Batch:
    """Create a tag and put it on a transaction, as one batch, so one undo removes both. Raises
    ValueError for a bad name or color, or a name that a tag already has."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    current = _transaction_values(session, txn_id)
    new_id, clean_name, created = new_tag_change(session, name, color)
    changes = [created, *link_changes(session, TransactionTag, add=[(txn_id, new_id)])]
    return _write(
        session,
        kind="register.tags",
        txn_id=txn_id,
        field="tags",
        label=f"Created tag {clean_name} on {current['payee']}",
        changes=changes,
        idempotency_key=idempotency_key,
    )


def edit_split_tags(
    session: Session, split_id: int, tag_ids: Sequence[int], *, idempotency_key: str
) -> Batch | None:
    """Set the tags of a split line. Returns None when it already has exactly these."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    split_row = session.get(Split, split_id)
    if split_row is None:
        raise LookupError(f"No such split {split_id}")
    changes = retag_changes(session, SplitTag, split_id, tag_ids)
    if not changes:
        return None
    return _write(
        session,
        kind="register.split_tags",
        txn_id=split_row.transaction_id,
        field="tags",
        label="Update split tags",
        changes=changes,
        idempotency_key=idempotency_key,
    )


def toggle_split_tag(
    session: Session, split_id: int, tag_id: int, *, idempotency_key: str
) -> Batch | None:
    """Put a tag on a split line, or take it off if it has it."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    current = set(session.scalars(select(SplitTag.tag_id).where(SplitTag.split_id == split_id)))
    wanted = current - {tag_id} if tag_id in current else current | {tag_id}
    return edit_split_tags(session, split_id, sorted(wanted), idempotency_key=idempotency_key)


def set_category(
    session: Session, txn_id: int, category_id: int | None, *, idempotency_key: str
) -> Batch:
    """Set the category, with origin manual. Clearing it (None) gives origin none.

    Raises ValueError for a split transaction, whose splits carry the categories, and for a
    category that does not exist or is hidden.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    current = _transaction_values(session, txn_id)
    if _split_count(session, txn_id):
        raise ValueError("This transaction is split. Change its split lines instead.")
    if category_id is None:
        origin = CategoryOrigin.none
        label = f"Clear category of {current['payee']}"
    else:
        name = _usable_category(session, category_id)
        origin = CategoryOrigin.manual
        label = f"Set category of {current['payee']} to {name}"
    before = {"category_id": current["category_id"], "category_origin": current["category_origin"]}
    after = {"category_id": category_id, "category_origin": origin.value}
    return _write(
        session,
        kind="register.category",
        txn_id=txn_id,
        field="category",
        label=label,
        changes=_field_changes(txn_id, before, after),
        idempotency_key=idempotency_key,
    )


def keep_category(session: Session, txn_id: int, *, idempotency_key: str) -> Batch:
    """Make the category a keyword or a rule gave the transaction your own choice, as if you had
    picked it. Nothing automatic changes it after that.

    Raises ValueError for a split transaction, one with no category, and one whose category you
    already chose.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    current = _transaction_values(session, txn_id)
    if _split_count(session, txn_id):
        raise ValueError("This transaction is split. Change its split lines instead.")
    category_id = current["category_id"]
    if category_id is None:
        raise ValueError("This transaction has no category to keep.")
    if current["category_origin"] == CategoryOrigin.manual.value:
        raise ValueError("You already chose this category.")
    name = _usable_category(session, int(category_id))
    before = {"category_id": category_id, "category_origin": current["category_origin"]}
    after = {"category_id": category_id, "category_origin": CategoryOrigin.manual.value}
    return _write(
        session,
        kind="register.category",
        txn_id=txn_id,
        field="category",
        label=f"Kept {name} for {current['payee']}",
        changes=_field_changes(txn_id, before, after),
        idempotency_key=idempotency_key,
    )


def set_splits(
    session: Session,
    txn_id: int,
    lines: Sequence[SplitLine],
    *,
    tags: Sequence[Sequence[int]] | None = None,
    idempotency_key: str,
) -> Batch:
    """Replace the splits with `lines`, each (amount_cents, category_id, memo).

    One batch: the old split rows and their tags are deleted, the new ones are inserted with ids
    that no row holds yet, and the transaction's own category is cleared. The lines must pass
    check_split_lines. Each split's origin is manual. A new line keeps the counterparty of the old
    line at the same position, so editing amounts or categories does not forget who a part is
    owed by. `tags` gives each new line its tag ids, in line order. Without it, a new line keeps
    the tags of the old line at the same position. Raises ValueError otherwise.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    current = _transaction_values(session, txn_id)
    check = check_split_lines(session, current["amount_cents"], lines)
    if check.lines is None:
        raise ValueError(next((error for error in check.line_errors if error), check.message))
    rows = check.lines

    old_ids = _split_ids(session, txn_id)
    old = [snapshot(session, "split", split_id) or {} for split_id in old_ids]
    if tags is None:
        carried = _split_tag_ids(session, old_ids)
        line_tags = [
            carried.get(old_ids[i], []) if i < len(old_ids) else [] for i in range(len(rows))
        ]
    else:
        if len(tags) != len(rows):
            raise ValueError("Give each split line its tags.")
        line_tags = [list(ids) for ids in tags]
        check_tags_exist(session, {tag_id for ids in line_tags for tag_id in ids})
    changes = split_tag_deletes(session, old_ids)
    changes += [
        Change("split", str(split_id), before, None)
        for split_id, before in zip(old_ids, old, strict=True)
    ]
    next_id = next_primary_key(session, Split.__table__)
    new_tags: list[tuple[int, int]] = []
    for position, (amount, category_id, memo) in enumerate(rows):
        new_tags.extend((next_id, tag_id) for tag_id in line_tags[position])
        kept = old[position] if position < len(old) else {}
        changes.append(
            Change(
                "split",
                str(next_id),
                None,
                {
                    "id": next_id,
                    "transaction_id": txn_id,
                    "position": position,
                    "amount_cents": amount,
                    "category_id": category_id,
                    "category_origin": CategoryOrigin.manual.value,
                    "memo": (memo or "").strip() or None,
                    "contact_id": kept.get("contact_id"),
                    "contact_account_id": kept.get("contact_account_id"),
                    "contact_origin": kept.get("contact_origin", ContactOrigin.none.value),
                },
            )
        )
        next_id += 1
    changes += link_changes(session, SplitTag, add=new_tags)
    before = {"category_id": current["category_id"], "category_origin": current["category_origin"]}
    after = {"category_id": None, "category_origin": CategoryOrigin.none.value}
    changes += _field_changes(txn_id, before, after)

    if old_ids:
        label = f"Change split of {current['payee']} to {len(rows)} lines"
    else:
        label = f"Split {current['payee']} into {len(rows)} lines"
    return _write(
        session,
        kind="register.split",
        txn_id=txn_id,
        field="split",
        label=label,
        changes=changes,
        idempotency_key=idempotency_key,
    )


def clear_splits(session: Session, txn_id: int, *, idempotency_key: str) -> Batch:
    """Delete every split, leaving the transaction unsplit and uncategorized.

    Raises ValueError if the transaction is not split.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    current = _transaction_values(session, txn_id)
    split_ids = _split_ids(session, txn_id)
    if not split_ids:
        raise ValueError("This transaction is not split.")
    changes = split_tag_deletes(session, split_ids)
    changes += [
        Change("split", str(split_id), snapshot(session, "split", split_id), None)
        for split_id in split_ids
    ]
    before = {"category_id": current["category_id"], "category_origin": current["category_origin"]}
    after = {"category_id": None, "category_origin": CategoryOrigin.none.value}
    changes += _field_changes(txn_id, before, after)
    return _write(
        session,
        kind="register.unsplit",
        txn_id=txn_id,
        field="split",
        label=f"Remove split from {current['payee']}",
        changes=changes,
        idempotency_key=idempotency_key,
    )


# Split checking


@dataclass(frozen=True)
class SplitDraft:
    """One split line as entered in the editor: the amount is text not yet read, and the category
    is the one picked, if any."""

    amount: str = ""
    category_id: int | None = None
    memo: str = ""
    tag_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class SplitBalance:
    """How much of a transaction typed split amounts account for. `left_cents` is None when an
    amount cannot be read, and `counted` is how many lines have an amount."""

    left_cents: int | None
    counted: int

    @property
    def can_save(self) -> bool:
        return self.left_cents == 0 and self.counted >= 2


@dataclass(frozen=True)
class SplitCheck:
    """What checking proposed split lines found.

    `lines` holds the accepted lines, or is None when anything is wrong, and `line_tags` has the
    tag ids of each accepted line. `line_errors` has one entry per proposed line, empty when the
    line is fine. `message` is the overall problem, empty when `lines` is set.
    """

    lines: list[SplitLine] | None
    line_errors: list[str]
    message: str
    balance: SplitBalance
    line_tags: list[tuple[int, ...]] = field(default_factory=list)


def split_balance(total_cents: int, amounts: Sequence[str]) -> SplitBalance:
    """The amount left after the typed amounts. Blank amounts count as nothing."""
    left = total_cents
    counted = 0
    for text in amounts:
        cleaned = text.strip()
        if not cleaned:
            continue
        try:
            left -= to_cents(cleaned)
        except ValueError:
            return SplitBalance(None, counted)
        counted += 1
    return SplitBalance(left, counted)


def check_split_drafts(
    session: Session, total_cents: int, drafts: Sequence[SplitDraft]
) -> SplitCheck:
    """Check split lines as entered. Blank lines are ignored. Every other line needs an amount and
    a category that exists and is not hidden, and the lines must satisfy the rules in
    check_split_lines."""
    errors: list[str] = []
    lines: list[SplitLine] = []
    line_tags: list[tuple[int, ...]] = []
    for draft in drafts:
        if not (draft.amount.strip() or draft.category_id is not None or draft.memo.strip()):
            errors.append("")
            continue
        try:
            cents = _read_amount(draft.amount)
            if draft.category_id is None:
                raise ValueError("Pick a category.")
            _usable_category(session, draft.category_id)
        except ValueError as exc:
            errors.append(str(exc))
            continue
        errors.append("")
        lines.append((cents, draft.category_id, draft.memo.strip() or None))
        line_tags.append(draft.tag_ids)
    balance = split_balance(total_cents, [draft.amount for draft in drafts])
    return _conclude_split_check(total_cents, errors, lines, balance, line_tags)


def check_split_lines(session: Session, total_cents: int, lines: Sequence[SplitLine]) -> SplitCheck:
    """Check split lines whose amounts and categories are already known.

    Each line needs a nonzero amount, of either sign, and a category that exists and is not
    hidden. There must be at least two lines, and they must add up to the transaction exactly.
    """
    errors: list[str] = []
    for amount, category_id, _memo in lines:
        try:
            _check_amount(amount)
            if category_id is None:
                raise ValueError("Pick a category.")
            _usable_category(session, category_id)
        except ValueError as exc:
            errors.append(str(exc))
        else:
            errors.append("")
    left = total_cents - sum(amount for amount, _, _ in lines)
    return _conclude_split_check(total_cents, errors, list(lines), SplitBalance(left, len(lines)))


def _conclude_split_check(
    total_cents: int,
    errors: list[str],
    lines: list[SplitLine],
    balance: SplitBalance,
    line_tags: list[tuple[int, ...]] | None = None,
) -> SplitCheck:
    """The whole-split rules, applied once every line is individually acceptable."""
    if any(errors):
        message = "Fix the lines marked in red."
    elif len(lines) < 2:
        message = "A split needs at least 2 lines."
    elif (unplaced := total_cents - sum(amount for amount, _, _ in lines)) != 0:
        message = (
            f"Lines must add up to {format_cents(total_cents)} exactly. "
            f"Amount left: {format_cents(unplaced)}."
        )
    else:
        return SplitCheck(lines, errors, "", balance, line_tags or [])
    return SplitCheck(None, errors, message, balance)


def _read_amount(text: str) -> int:
    cleaned = text.strip()
    if not cleaned:
        raise ValueError("Enter an amount.")
    try:
        cents = to_cents(cleaned)
    except ValueError:
        raise ValueError(f"{cleaned} is not an amount.") from None
    _check_amount(cents)
    return cents


def _check_amount(cents: int) -> None:
    if cents == 0:
        raise ValueError("An amount cannot be zero.")


# Helpers


def _register_query() -> Select:
    return (
        select(Transaction, Account, Category)
        .select_from(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .outerjoin(Category, Category.id == Transaction.category_id)
    )


def _rows(
    session: Session, found: Sequence[tuple[Transaction, Account, Category | None]]
) -> list[RegisterRow]:
    ids = [txn.id for txn, _, _ in found]
    splits = _splits_for(session, ids)
    links = _links_for(session, ids)
    tags = _tags_on(session, TransactionTag, ids)
    return [
        RegisterRow(
            id=txn.id,
            posted_on=txn.posted_on,
            account_id=account.id,
            account_name=account.name,
            account_digits=account_digits(account.last4, account.alias_last4),
            payee=txn.payee,
            category_id=txn.category_id,
            category=None if category is None else category.name,
            category_origin=txn.category_origin,
            amount_cents=txn.amount_cents,
            memo=txn.memo,
            splits=splits.get(txn.id, []),
            links=links.get(txn.id, []),
            tags=tags.get(txn.id, []),
        )
        for txn, account, category in found
    ]


def _links_for(session: Session, txn_ids: list[int]) -> dict[int, list[RowLink]]:
    """Each transaction's links. A receipt with several charges on one transaction shows once."""
    if not txn_ids:
        return {}
    found = session.scalars(
        select(Link)
        .where(or_(Link.from_transaction_id.in_(txn_ids), Link.to_transaction_id.in_(txn_ids)))
        .order_by(Link.id)
    ).all()
    other_ids = {
        txn_id
        for link in found
        for txn_id in (link.from_transaction_id, link.to_transaction_id)
        if txn_id is not None
    }
    others = {
        txn.id: LinkedTransaction(
            id=txn.id,
            posted_on=txn.posted_on,
            account_id=account.id,
            account_name=account.name,
            account_digits=account_digits(account.last4, account.alias_last4),
            amount_cents=txn.amount_cents,
        )
        for txn, account in session.execute(
            select(Transaction, Account)
            .join(Account, Account.id == Transaction.account_id)
            .where(Transaction.id.in_(other_ids))
        )
    }
    wanted = set(txn_ids)
    result: dict[int, list[RowLink]] = {}
    for link in found:
        kind = LinkKind(link.kind)
        description = (link.evidence or {}).get("description")
        label = description if isinstance(description, str) and description else LINK_LABELS[kind]
        ends = (link.from_transaction_id, link.to_transaction_id)
        for own, other in (ends, ends[::-1]):
            if own is None or own not in wanted:
                continue
            row_link = RowLink(
                kind,
                LINK_LABELS[kind] if kind is LinkKind.receipt else label,
                None if other is None else others.get(other),
                _receipt_of(link.receipt_key),
            )
            rows = result.setdefault(own, [])
            if row_link not in rows:
                rows.append(row_link)
    return result


def _receipt_of(receipt_key: str | None) -> str | None:
    """The receipt part of a charge key ("amazon:113-42:0" is "amazon:113-42"), so the charges of
    one receipt show as one link."""
    if receipt_key is None:
        return None
    head, _, last = receipt_key.rpartition(":")
    return head if last.isdigit() and head else receipt_key


def _splits_for(session: Session, txn_ids: list[int]) -> dict[int, list[SplitRow]]:
    if not txn_ids:
        return {}
    found = session.execute(
        select(Split, Category.name)
        .select_from(Split)
        .outerjoin(Category, Category.id == Split.category_id)
        .where(Split.transaction_id.in_(txn_ids))
        .order_by(Split.transaction_id, Split.position)
    ).all()
    split_ids = [split.id for split, _ in found]
    split_tags = _tags_on(session, SplitTag, split_ids)
    grouped: dict[int, list[SplitRow]] = {}
    for split, category_name in found:
        grouped.setdefault(split.transaction_id, []).append(
            SplitRow(
                split.id,
                split.amount_cents,
                split.category_id,
                category_name,
                split.memo,
                tags=split_tags.get(split.id, []),
            )
        )
    return grouped


def _tags_on(session: Session, link: TagLink, owner_ids: list[int]) -> dict[int, list[TagRow]]:
    """The tags that transactions or split lines carry, by owner id, ordered by name."""
    if not owner_ids:
        return {}
    owner = link_owner(link)
    found = session.execute(
        select(owner, Tag.id, Tag.name, Tag.color)
        .join(Tag, Tag.id == link.tag_id)
        .where(owner.in_(owner_ids))
        .order_by(func.lower(Tag.name))
    ).all()
    grouped: dict[int, list[TagRow]] = {}
    for owner_id, tag_id, name, color in found:
        grouped.setdefault(owner_id, []).append(TagRow(tag_id, name, color))
    return grouped


def _split_tag_ids(session: Session, split_ids: list[int]) -> dict[int, list[int]]:
    grouped: dict[int, list[int]] = {}
    if split_ids:
        for split_id, tag_id in session.execute(
            select(SplitTag.split_id, SplitTag.tag_id).where(SplitTag.split_id.in_(split_ids))
        ):
            grouped.setdefault(split_id, []).append(tag_id)
    return grouped


def _split_ids(session: Session, txn_id: int) -> list[int]:
    return list(
        session.scalars(
            select(Split.id).where(Split.transaction_id == txn_id).order_by(Split.position)
        ).all()
    )


def _split_count(session: Session, txn_id: int) -> int:
    return int(
        session.scalar(select(func.count(Split.id)).where(Split.transaction_id == txn_id)) or 0
    )


def _transaction_values(session: Session, txn_id: int) -> dict[str, object]:
    values = snapshot(session, "transaction", txn_id)
    if values is None:
        raise LookupError(f"there is no transaction {txn_id}")
    return values


def _usable_category(session: Session, category_id: int) -> str:
    """Return the category's name. Raises ValueError if it does not exist or is hidden."""
    row = session.execute(
        select(Category.name, Category.hidden).where(Category.id == category_id)
    ).first()
    if row is None:
        raise ValueError(f"There is no category {category_id}.")
    if row.hidden:
        raise ValueError(f"{row.name} is hidden, so it cannot be used.")
    return str(row.name)


def _field_changes(
    txn_id: int, before: dict[str, object], after: dict[str, object]
) -> list[Change]:
    """One change to the transaction's fields, or none when the values are already equal."""
    if before == after:
        return []
    return [Change("transaction", str(txn_id), before, after)]


def _write(
    session: Session,
    *,
    kind: str,
    txn_id: int,
    field: str,
    label: str,
    changes: list[Change],
    idempotency_key: str,
) -> Batch:
    preview = make_preview(
        session,
        kind,
        changes,
        summary={"label": label, "transaction_id": txn_id, "field": field},
    )
    return apply(session, preview, idempotency_key=idempotency_key)
