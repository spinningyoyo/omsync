"""Import: turning a parsed statement into a previewable, atomic, undoable batch.

Each statement row joins a known transaction, creates a new one, or goes to review. Matching
runs in three passes over the file, and none of them writes anything:

1. FITID. A row whose format trusts its FITID joins the transaction that an earlier row with
   the same FITID is linked to, if the amount and date still agree. If they do not, the row
   goes to review.
2. Fingerprint, by count. Identical rows are matched by count: each row joins the next existing
   transaction with its fingerprint, in file order. A transaction whose trusted FITID differs from
   the row's is never joined, because two trusted FITIDs name two transactions. Rows beyond the
   matches are new.
3. Correction guard. A new row that resembles an existing transaction in the file's period
   (same amount, dated within three days) goes to review instead of being created. A transaction
   with a different trusted FITID is known to be a different one, so it does not count as a
   resemblance. Otherwise the row is created.

The passes run in that order because the guard must know every transaction this file joins.
Pre-assigned primary keys make the preview self-contained: apply writes exactly what plan
computed, and rejects the preview if the data has changed since.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    BatchError,
    Change,
    Preview,
    applied_batch_for_key,
    apply,
    make_preview,
    next_primary_key,
    snapshot,
)
from omsync.ingest.formats.base import (
    MatchEvidence,
    ParsedRow,
    Statement,
    StatementFormat,
    fingerprint,
)
from omsync.ledger.accounts import AccountError, UnmappedAccount, find_account, last4_of
from omsync.ledger.models import (
    Account,
    CategoryOrigin,
    ImportReview,
    Institution,
    MatchKind,
    ReviewStatus,
    StatementBalance,
    StatementFile,
    StatementRow,
    Transaction,
    TransactionRow,
)
from omsync.ledger.statements import statement_description

DATE_WINDOW = timedelta(days=3)

REASON_CHANGED = "bank changed amount or date for a known transaction"
REASON_CORRECTION = "possible correction of an existing transaction"
REASON_REPEATED_FITID = "the file repeats a FITID that no known transaction has"
REASON_AMBIGUOUS_FITID = "a FITID matches more than one transaction"


@dataclass(frozen=True)
class ImportPlan:
    """What importing a statement file would do. Nothing is written until apply_import.

    preview is None when the file was already imported, so re-importing changes nothing.
    review_reasons has one entry per row sent to review, in file order.
    """

    preview: Preview | None
    already_imported: bool
    account_id: int
    joined: int
    created: int
    review: int
    review_reasons: tuple[str, ...]


@dataclass(frozen=True)
class _Join:
    transaction_id: int
    match_kind: MatchKind


@dataclass(frozen=True)
class _Create:
    transaction_id: int  # pre-assigned, so later changes in the same batch can refer to it


@dataclass(frozen=True)
class _Review:
    reason: str
    candidates: tuple[int, ...]


_Decision = _Join | _Create | _Review


@dataclass(frozen=True)
class _Linked:
    """A statement row already linked to a transaction, with the values the bank reported."""

    statement_row_id: int
    transaction_id: int
    amount_cents: int
    posted_on: date


@dataclass(frozen=True)
class _Existing:
    """A transaction in the account, as much as matching needs to know about it.

    trusted_fitids are the FITIDs its linked statement rows carry that their formats vouched for.
    """

    transaction_id: int
    amount_cents: int
    posted_on: date
    trusted_fitids: frozenset[str] = frozenset()


def plan_import(
    session: Session,
    statement: Statement,
    fmt: StatementFormat,
    *,
    institution_key: str,
    stored_path: str,
    original_name: str,
) -> ImportPlan:
    """Plan an import without writing anything. See the module docstring for the rules.

    Raises UnmappedAccount if the statement's account is not mapped to an account at the
    institution.
    """
    imported = session.execute(
        select(StatementFile.account_id).where(StatementFile.sha256 == statement.sha256)
    ).first()
    if imported is not None:
        if imported.account_id is None:
            raise AccountError("this statement file is recorded without an account")
        return ImportPlan(
            preview=None,
            already_imported=True,
            account_id=imported.account_id,
            joined=0,
            created=0,
            review=0,
            review_reasons=(),
        )

    account = find_account(session, institution_key, statement.statement_account_id)
    if account is None:
        raise UnmappedAccount(institution_key, last4_of(statement.statement_account_id))

    evidence = {
        row.position: _match_evidence(fmt, statement, row, account_key=str(account.id))
        for row in statement.rows
    }
    decisions = _decide(session, account.id, statement, evidence)

    in_order = [decisions[row.position] for row in statement.rows]
    joined = sum(isinstance(decision, _Join) for decision in in_order)
    created = sum(isinstance(decision, _Create) for decision in in_order)
    review_reasons = tuple(
        decision.reason for decision in in_order if isinstance(decision, _Review)
    )
    changes = _import_changes(
        session,
        account.id,
        statement,
        stored_path,
        original_name,
        evidence,
        decisions,
        institution_key=institution_key,
    )
    preview = make_preview(
        session,
        "import",
        changes,
        summary={
            "file": original_name,
            "joined": joined,
            "created": created,
            "review": len(review_reasons),
        },
    )
    return ImportPlan(
        preview=preview,
        already_imported=False,
        account_id=account.id,
        joined=joined,
        created=created,
        review=len(review_reasons),
        review_reasons=review_reasons,
    )


def apply_import(session: Session, plan: ImportPlan, *, idempotency_key: str) -> Batch:
    """Apply a planned import as one batch. The same key applies at most once."""
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    if plan.preview is None:
        raise BatchError("nothing to import: this statement file was already imported")
    return apply(session, plan.preview, idempotency_key=idempotency_key)


def resolve_review(
    session: Session,
    review_id: int,
    *,
    transaction_id: int | None,
    idempotency_key: str,
) -> Batch:
    """Settle one row in review, as one batch.

    With transaction_id, the row joins that transaction, which must be in the same account.
    Without it, the row becomes a new transaction. Either way the review is marked resolved.
    Raises BatchError if the review is unknown or already resolved.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    review = session.get(ImportReview, review_id)
    if review is None:
        raise BatchError(f"there is no import review {review_id}")
    if review.status is not ReviewStatus.open:
        raise BatchError(f"import review {review_id} is already resolved")
    row = session.get(StatementRow, review.statement_row_id)
    if row is None:
        raise BatchError(f"import review {review_id} has no statement row")
    statement_file = session.get(StatementFile, row.statement_file_id)
    account_id = None if statement_file is None else statement_file.account_id
    if account_id is None:
        raise AccountError(f"import review {review_id} has no account to post to")
    if session.get(TransactionRow, row.id) is not None:
        raise BatchError(f"the statement row for import review {review_id} is already linked")

    changes: list[Change] = []
    if transaction_id is None:
        institution_key = session.scalar(
            select(Institution.key)
            .join(Account, Account.institution_id == Institution.id)
            .where(Account.id == account_id)
        )
        linked_id = next_primary_key(session, Transaction.__table__)
        changes.append(
            _transaction_change(
                linked_id,
                account_id,
                posted_on=row.posted_on,
                amount_cents=row.amount_cents,
                description=statement_description(
                    institution_key, statement_file.format_key, row.payee_raw, row.memo_raw
                ),
            )
        )
        match_kind = MatchKind.created
    else:
        target = session.get(Transaction, transaction_id)
        if target is None or target.account_id != account_id:
            raise BatchError(f"transaction {transaction_id} is not in this review's account")
        linked_id = transaction_id
        match_kind = MatchKind.manual

    changes.append(
        Change(
            "transaction_row",
            str(row.id),
            None,
            {
                "statement_row_id": row.id,
                "transaction_id": linked_id,
                "match_kind": match_kind.value,
            },
        )
    )
    before = snapshot(session, "import_review", review_id)
    if before is None:
        raise BatchError(f"there is no import review {review_id}")
    after = {
        **before,
        "status": ReviewStatus.resolved.value,
        "resolved_transaction_id": linked_id,
    }
    changes.append(Change("import_review", str(review_id), before, after))
    preview = make_preview(
        session,
        "review_resolve",
        changes,
        summary={"review": review_id, "created": transaction_id is None},
    )
    return apply(session, preview, idempotency_key=idempotency_key)


# Matching


def _decide(
    session: Session,
    account_id: int,
    statement: Statement,
    evidence: dict[int, MatchEvidence],
) -> dict[int, _Decision]:
    decisions: dict[int, _Decision] = {}
    claimed: set[int] = set()
    _match_fitids(session, account_id, statement.rows, evidence, decisions, claimed)
    _match_fingerprints(session, account_id, statement.rows, evidence, decisions, claimed)
    _match_or_create(session, account_id, statement, evidence, decisions, claimed)
    return decisions


def _match_fitids(
    session: Session,
    account_id: int,
    rows: Iterable[ParsedRow],
    evidence: dict[int, MatchEvidence],
    decisions: dict[int, _Decision],
    claimed: set[int],
) -> None:
    """Join rows whose trusted FITID is already linked to a transaction in this account."""
    row_list = list(rows)
    fitids = {row.position: _trusted_fitid(evidence[row.position]) for row in row_list}
    counts = Counter(fitid for fitid in fitids.values() if fitid is not None)
    known = _linked_by_fitid(session, account_id, set(counts))

    for row in row_list:
        fitid = fitids[row.position]
        if fitid is None:
            continue
        links = known.get(fitid, [])
        if not links:
            # A repeated FITID with no known transaction cannot be resolved from the evidence.
            if counts[fitid] > 1:
                decisions[row.position] = _Review(REASON_REPEATED_FITID, ())
            continue
        transactions = tuple(sorted({link.transaction_id for link in links}))
        if len(transactions) > 1:
            decisions[row.position] = _Review(REASON_AMBIGUOUS_FITID, transactions)
            continue
        latest = max(links, key=lambda link: link.statement_row_id)
        if latest.amount_cents == row.amount_cents and latest.posted_on == row.posted_on:
            decisions[row.position] = _Join(transactions[0], MatchKind.fitid)
            claimed.add(transactions[0])
        else:
            decisions[row.position] = _Review(REASON_CHANGED, transactions)


def _match_fingerprints(
    session: Session,
    account_id: int,
    rows: Iterable[ParsedRow],
    evidence: dict[int, MatchEvidence],
    decisions: dict[int, _Decision],
    claimed: set[int],
) -> None:
    """Join undecided rows to existing transactions by fingerprint, one row per transaction.

    Rows with a fingerprint take the transactions that already have it, in file order. A
    transaction joined by an earlier pass is not offered again. A transaction whose trusted FITID
    differs from the row's trusted FITID is not offered either: two trusted FITIDs name two
    transactions, so a fingerprint alone must not merge them.
    """
    groups: dict[str, list[ParsedRow]] = {}
    for row in rows:
        if row.position not in decisions:
            groups.setdefault(evidence[row.position].fingerprint, []).append(row)
    linked = _linked_by_fingerprint(session, account_id, set(groups))
    trusted = _trusted_fitids_by_transaction(
        session, {txn for transaction_ids in linked.values() for txn in transaction_ids}
    )

    for key, group in groups.items():
        free = [txn for txn in linked.get(key, []) if txn not in claimed]
        for row in group:
            fitid = _trusted_fitid(evidence[row.position])
            candidate = next(
                (
                    txn
                    for txn in free
                    if txn not in claimed
                    and not _identity_conflicts(fitid, trusted.get(txn, frozenset()))
                ),
                None,
            )
            if candidate is not None:
                decisions[row.position] = _Join(candidate, MatchKind.fingerprint)
                claimed.add(candidate)


def _match_or_create(
    session: Session,
    account_id: int,
    statement: Statement,
    evidence: dict[int, MatchEvidence],
    decisions: dict[int, _Decision],
    claimed: set[int],
) -> None:
    """Send each undecided row to review if it may correct a known transaction, else create it.

    A transaction whose trusted FITID differs from the row's is known to be a different
    transaction, so it is never a correction candidate.
    """
    pending = [row for row in statement.rows if row.position not in decisions]
    if not pending:
        return
    first = min(row.posted_on for row in pending)
    last = max(row.posted_on for row in pending)
    period_start = statement.period_start or first
    period_end = statement.period_end or last
    existing = _existing_in_window(
        session,
        account_id,
        lower=max(period_start, first - DATE_WINDOW),
        upper=min(period_end, last + DATE_WINDOW),
    )

    next_id = next_primary_key(session, Transaction.__table__)
    for row in pending:
        fitid = _trusted_fitid(evidence[row.position])
        near = tuple(
            sorted(
                txn.transaction_id
                for txn in existing
                if txn.transaction_id not in claimed
                and txn.amount_cents == row.amount_cents
                and period_start <= txn.posted_on <= period_end
                and abs((txn.posted_on - row.posted_on).days) <= DATE_WINDOW.days
                and not _identity_conflicts(fitid, txn.trusted_fitids)
            )
        )
        if near:
            decisions[row.position] = _Review(REASON_CORRECTION, near)
        else:
            decisions[row.position] = _Create(next_id)
            next_id += 1


def _match_evidence(
    fmt: StatementFormat, statement: Statement, row: ParsedRow, *, account_key: str
) -> MatchEvidence:
    """The format's evidence, with the fingerprint recomputed from the omsync account.

    A format's fingerprint is keyed by the statement account ID, which changes when a card is
    reissued. Keying by the account instead lets every alias of one account match the same
    transactions. Only the format's FITID and its trust flag are taken from the evidence.
    """
    given = fmt.evidence(statement, row)
    return replace(
        given,
        fingerprint=fingerprint(account_key, row.posted_on, row.amount_cents, row.payee_raw),
    )


def _trusted_fitid(evidence: MatchEvidence) -> str | None:
    return evidence.fitid if evidence.fitid_trusted and evidence.fitid else None


def _identity_conflicts(fitid: str | None, known: frozenset[str] | set[str]) -> bool:
    """True when a row's trusted FITID is not among the trusted FITIDs of a transaction that has
    some. Both sides vouched for their IDs, so the row and the transaction are different ones."""
    return fitid is not None and bool(known) and fitid not in known


def _trusted_fitids_by_transaction(
    session: Session, transaction_ids: set[int]
) -> dict[int, frozenset[str]]:
    """Map each transaction to the trusted FITIDs of its linked statement rows. Transactions with
    none are absent."""
    if not transaction_ids:
        return {}
    rows = session.execute(
        select(TransactionRow.transaction_id, StatementRow.fitid)
        .join(StatementRow, StatementRow.id == TransactionRow.statement_row_id)
        .where(
            TransactionRow.transaction_id.in_(sorted(transaction_ids)),
            StatementRow.fitid_trusted.is_(True),
            StatementRow.fitid.is_not(None),
        )
    ).all()
    known: dict[int, set[str]] = {}
    for transaction_id, fitid in rows:
        known.setdefault(transaction_id, set()).add(fitid)
    return {txn: frozenset(fitids) for txn, fitids in known.items()}


def _linked_by_fitid(
    session: Session, account_id: int, fitids: set[str]
) -> dict[str, list[_Linked]]:
    if not fitids:
        return {}
    rows = session.execute(
        select(
            StatementRow.fitid,
            StatementRow.id,
            TransactionRow.transaction_id,
            StatementRow.amount_cents,
            StatementRow.posted_on,
        )
        .select_from(StatementRow)
        .join(TransactionRow, TransactionRow.statement_row_id == StatementRow.id)
        .join(StatementFile, StatementFile.id == StatementRow.statement_file_id)
        .where(StatementFile.account_id == account_id, StatementRow.fitid.in_(sorted(fitids)))
    ).all()
    linked: dict[str, list[_Linked]] = {}
    for fitid, statement_row_id, transaction_id, amount_cents, posted_on in rows:
        linked.setdefault(fitid, []).append(
            _Linked(statement_row_id, transaction_id, amount_cents, posted_on)
        )
    return linked


def _linked_by_fingerprint(
    session: Session, account_id: int, fingerprints: set[str]
) -> dict[str, list[int]]:
    """Map each fingerprint to the account's transactions with a linked row that has it, by ID."""
    if not fingerprints:
        return {}
    rows = session.execute(
        select(StatementRow.fingerprint, Transaction.id)
        .select_from(StatementRow)
        .join(TransactionRow, TransactionRow.statement_row_id == StatementRow.id)
        .join(Transaction, Transaction.id == TransactionRow.transaction_id)
        .where(
            Transaction.account_id == account_id,
            StatementRow.fingerprint.in_(sorted(fingerprints)),
        )
        .distinct()
        .order_by(Transaction.id)
    ).all()
    linked: dict[str, list[int]] = {}
    for key, transaction_id in rows:
        linked.setdefault(key, []).append(transaction_id)
    return linked


def _existing_in_window(
    session: Session, account_id: int, *, lower: date, upper: date
) -> list[_Existing]:
    if lower > upper:
        return []
    rows = session.execute(
        select(Transaction.id, Transaction.amount_cents, Transaction.posted_on).where(
            Transaction.account_id == account_id,
            Transaction.posted_on.between(lower, upper),
        )
    ).all()
    trusted = _trusted_fitids_by_transaction(session, {row.id for row in rows})
    return [
        _Existing(txn_id, amount, posted_on, trusted.get(txn_id, frozenset()))
        for txn_id, amount, posted_on in rows
    ]


# Changes


def _import_changes(
    session: Session,
    account_id: int,
    statement: Statement,
    stored_path: str,
    original_name: str,
    evidence: dict[int, MatchEvidence],
    decisions: dict[int, _Decision],
    *,
    institution_key: str,
) -> list[Change]:
    """The inserts for one import, in dependency order: the file, its rows and balances, new
    transactions, the links from rows to transactions, then the review entries."""
    file_id = next_primary_key(session, StatementFile.__table__)
    row_base = next_primary_key(session, StatementRow.__table__)
    balance_base = next_primary_key(session, StatementBalance.__table__)
    review_base = next_primary_key(session, ImportReview.__table__)
    row_ids = {row.position: row_base + index for index, row in enumerate(statement.rows)}

    return [
        _file_change(statement, file_id, account_id, stored_path, original_name),
        *_row_changes(statement, file_id, row_ids, evidence),
        *_balance_changes(statement, file_id, account_id, balance_base),
        *_created_transaction_changes(statement, account_id, decisions, institution_key),
        *_link_changes(statement, row_ids, decisions),
        *_review_changes(statement, review_base, row_ids, decisions),
    ]


def _file_change(
    statement: Statement, file_id: int, account_id: int, stored_path: str, original_name: str
) -> Change:
    return Change(
        "statement_file",
        str(file_id),
        None,
        {
            "id": file_id,
            "sha256": statement.sha256,
            "format_key": statement.format_key,
            "original_name": original_name,
            "stored_path": stored_path,
            "statement_account_id": statement.statement_account_id,
            "account_id": account_id,
            "period_start": statement.period_start,
            "period_end": statement.period_end,
        },
    )


def _row_changes(
    statement: Statement,
    file_id: int,
    row_ids: dict[int, int],
    evidence: dict[int, MatchEvidence],
) -> list[Change]:
    return [
        Change(
            "statement_row",
            str(row_ids[row.position]),
            None,
            {
                "id": row_ids[row.position],
                "statement_file_id": file_id,
                "position": row.position,
                "posted_on": row.posted_on,
                "amount_cents": row.amount_cents,
                "payee_raw": row.payee_raw,
                "memo_raw": row.memo_raw,
                "fitid": row.fitid,
                "fitid_trusted": _trusted_fitid(evidence[row.position]) is not None,
                "fingerprint": evidence[row.position].fingerprint,
                "raw": dict(row.raw),
            },
        )
        for row in statement.rows
    ]


def _balance_changes(
    statement: Statement, file_id: int, account_id: int, balance_base: int
) -> list[Change]:
    return [
        Change(
            "statement_balance",
            str(balance_base + index),
            None,
            {
                "id": balance_base + index,
                "account_id": account_id,
                "statement_file_id": file_id,
                "as_of": balance.as_of,
                "balance_cents": balance.balance_cents,
                "kind": balance.kind,
            },
        )
        for index, balance in enumerate(statement.balances)
    ]


def _created_transaction_changes(
    statement: Statement,
    account_id: int,
    decisions: dict[int, _Decision],
    institution_key: str,
) -> list[Change]:
    changes: list[Change] = []
    for row in statement.rows:
        decision = decisions[row.position]
        if isinstance(decision, _Create):
            changes.append(
                _transaction_change(
                    decision.transaction_id,
                    account_id,
                    posted_on=row.posted_on,
                    amount_cents=row.amount_cents,
                    description=statement_description(
                        institution_key, statement.format_key, row.payee_raw, row.memo_raw
                    ),
                )
            )
    return changes


def _link_changes(
    statement: Statement, row_ids: dict[int, int], decisions: dict[int, _Decision]
) -> list[Change]:
    changes: list[Change] = []
    for row in statement.rows:
        decision = decisions[row.position]
        if isinstance(decision, _Join):
            match_kind = decision.match_kind
        elif isinstance(decision, _Create):
            match_kind = MatchKind.created
        else:
            continue
        changes.append(
            Change(
                "transaction_row",
                str(row_ids[row.position]),
                None,
                {
                    "statement_row_id": row_ids[row.position],
                    "transaction_id": decision.transaction_id,
                    "match_kind": match_kind.value,
                },
            )
        )
    return changes


def _review_changes(
    statement: Statement,
    review_base: int,
    row_ids: dict[int, int],
    decisions: dict[int, _Decision],
) -> list[Change]:
    reviews: list[tuple[ParsedRow, _Review]] = []
    for row in statement.rows:
        decision = decisions[row.position]
        if isinstance(decision, _Review):
            reviews.append((row, decision))
    return [
        Change(
            "import_review",
            str(review_base + offset),
            None,
            {
                "id": review_base + offset,
                "statement_row_id": row_ids[row.position],
                "reason": decision.reason,
                "candidate_transaction_ids": list(decision.candidates),
                "status": ReviewStatus.open.value,
                "resolved_transaction_id": None,
            },
        )
        for offset, (row, decision) in enumerate(reviews)
    ]


def _transaction_change(
    transaction_id: int,
    account_id: int,
    *,
    posted_on: date,
    amount_cents: int,
    description: str,
) -> Change:
    """A new transaction. Its memo starts empty: the memo is yours, and a statement's MEMO stays on
    the statement row."""
    values: dict[str, Any] = {
        "id": transaction_id,
        "account_id": account_id,
        "posted_on": posted_on,
        "amount_cents": amount_cents,
        "payee": description.strip(),
        "payee_raw": description,
        "memo": None,
        "category_id": None,
        "category_origin": CategoryOrigin.none.value,
    }
    return Change("transaction", str(transaction_id), None, values)
