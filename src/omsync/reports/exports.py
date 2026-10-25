"""Excel exports for one calendar year: the transactions workbook and the rental workbook.

Both read the ledger and write one .xlsx file. Neither changes data. Money is written as dollars
with two decimals, built from integer cents by dollars(), so no float reaches a cell. Exports
show full amounts: joint share is off, and the joint column says which categories include
joint-account activity.

A file goes to the exports directory unless the caller passes an explicit path, which is the only
way to write elsewhere. Missing directories are created.
"""

from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from omsync.ledger.categories import NOT_IN_REPORTS, TYPE_LABELS
from omsync.ledger.models import Account, Category, Link, Transaction
from omsync.ledger.sources import account_digits
from omsync.reports.queries import (
    Period,
    by_category,
    counted_amount,
    counted_splits,
    explained_cents,
    summary,
    uncategorized_total,
)
from omsync.reports.rental import get_rental_accounts, get_rental_categories

MONEY_FORMAT = "#,##0.00"
DATE_FORMAT = "yyyy-mm-dd"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
UNCATEGORIZED = "(uncategorized)"
SPLIT_PARENT = "(split)"
OTHER = "Other"
TRANSFERS = "Transfers and loans (not in reports)"
INCOME = "Income"
EXPENSES = "Expenses"
SHEET_TITLE_LIMIT = 31
_RESERVED_SHEET_TITLES = ("Summary", "Transactions")
_HEADER_FILL = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")

TRANSACTION_COLUMNS = (
    "row_type",
    "transaction_id",
    "split_id",
    "date",
    "account",
    "payee",
    "amount",
    "category",
    "origin",
    "memo",
    "links",
)
TRANSACTION_WIDTHS = (12, 14, 10, 12, 12, 28, 14, 24, 12, 30, 34)
CATEGORY_COLUMNS = ("category_type", "category", "amount", "joint")
CATEGORY_WIDTHS = (18, 30, 14, 8)
RENTAL_DETAIL_COLUMNS = (
    "property",
    "date",
    "account",
    "payee",
    "memo",
    "transaction_id",
    "split_id",
    "amount",
    "category",
    "bucket",
)
RENTAL_DETAIL_WIDTHS = (22, 12, 12, 28, 28, 14, 10, 14, 24, 22)
SUMMARY_COLUMNS = (
    "property",
    "account",
    "income",
    "expenses",
    "net",
    "transfers and loans (not in reports)",
)
SUMMARY_WIDTHS = (24, 14, 14, 14, 14, 22)


class NoRentalAccounts(ValueError):
    """The rental export needs at least one account marked as a rental property."""


@dataclass(frozen=True)
class _Property:
    account_id: int
    label: str
    account: str  # the account's ••last4, or a note when the account is gone


@dataclass(frozen=True)
class _Line:
    """One line of a rental account: the counted part of a split or unsplit transaction, or its
    explained part, which is excluded."""

    account_id: int
    account: str
    transaction_id: int
    split_id: int | None
    posted_on: date
    payee: str
    memo: str | None
    cents: int
    category: str | None
    excluded: bool  # a transfer, joint settlement, or excluded line: not operating results
    bucket: str  # TRANSFERS when excluded, else the rental category name, else OTHER


def dollars(cents: int) -> Decimal:
    """The exact dollar amount of integer cents. For example, -5 becomes Decimal("-0.05")."""
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise TypeError(f"cents must be an int, not {type(cents).__name__}")
    sign = "-" if cents < 0 else ""
    whole, fraction = divmod(abs(cents), 100)
    return Decimal(f"{sign}{whole}.{fraction:02d}")


def export_transactions(
    session: Session, year: int, path: Path | None = None, *, exports_dir: Path
) -> Path:
    """Write the year's transactions workbook and return its path.

    The Transactions sheet has a parent row for each transaction, followed by an indented row for
    each split. The By category sheet has the year's spending per category with joint share off,
    as the reports count it, then the year's income, spending, net, and uncategorized total.
    """
    period = _year_period(year)
    target = _destination(path, exports_dir, f"transactions_{year}.xlsx")
    rows = _transactions(session, period)
    names = _category_names(session)
    links = _link_summaries(session, period, {txn.id for txn, _ in rows})

    workbook = Workbook()
    transactions = workbook.active
    if transactions is None:  # a new workbook always has its first sheet
        raise RuntimeError("a new workbook has no active sheet")
    transactions.title = "Transactions"
    _transactions_sheet(transactions, rows, names, links)
    _category_sheet(
        workbook.create_sheet("By category"),
        session,
        period,
        _joint_category_ids(rows),
    )
    workbook.save(target)
    return target


def export_rental(
    session: Session, year: int, path: Path | None = None, *, exports_dir: Path
) -> Path:
    """Write the year's rental workbook and return its path.

    Income and expenses are operating results only, and they count what the reports count: a
    transaction counts the amount its transfer and joint settlement links leave unexplained, and a
    split shares that counted amount in proportion (see queries.py). Joint share is off, as in the
    other exports. The explained part is excluded, as are lines in a category left out of reports,
    since owner contributions and distributions are not operating results. Those lines sit on a
    "Transfers and loans (not in reports)" row below each property's totals, and in the Summary's
    last column.
    So for each property, income minus expenses plus the transfers row is the sum of every ledger
    line in its account for the year.

    Each property has its own sheet with income and expenses per rental category and for Other,
    by month. Raises NoRentalAccounts when no account is set as rental.
    """
    accounts = get_rental_accounts(session)
    if not accounts:
        raise NoRentalAccounts("no rental accounts are set")
    categories = get_rental_categories(session)
    period = _year_period(year)
    target = _destination(path, exports_dir, f"rental_{year}.xlsx")

    properties = _properties(session, accounts)
    rows = _transactions(session, period, [prop.account_id for prop in properties])
    lines = _rental_lines(
        rows,
        _category_names(session),
        set(categories),
        explained=explained_cents(session, period),
        excluded_category_ids=_excluded_category_ids(session),
    )
    by_account: dict[int, list[_Line]] = defaultdict(list)
    for line in lines:
        by_account[line.account_id].append(line)

    workbook = Workbook()
    summary_sheet = workbook.active
    if summary_sheet is None:  # a new workbook always has its first sheet
        raise RuntimeError("a new workbook has no active sheet")
    summary_sheet.title = "Summary"
    _rental_summary_sheet(summary_sheet, properties, by_account)
    taken = {title.casefold() for title in _RESERVED_SHEET_TITLES}
    for prop in properties:
        sheet = workbook.create_sheet(_sheet_title(prop, taken))
        _property_sheet(sheet, by_account.get(prop.account_id, []), categories)
    _rental_detail_sheet(workbook.create_sheet("Transactions"), properties, by_account)
    workbook.save(target)
    return target


# Transactions workbook


def _transactions_sheet(
    ws: Worksheet,
    rows: Sequence[tuple[Transaction, Account]],
    names: dict[int, str],
    links: dict[int, list[str]],
) -> None:
    _start_sheet(ws, TRANSACTION_COLUMNS, TRANSACTION_WIDTHS, freeze="A2")
    category_col = TRANSACTION_COLUMNS.index("category") + 1
    for txn, account in rows:
        label = _account_label(account)
        category = SPLIT_PARENT if txn.splits else _category_name(names, txn.category_id)
        ws.append(
            [
                "transaction",
                txn.id,
                None,
                txn.posted_on,
                label,
                txn.payee,
                dollars(txn.amount_cents),
                category,
                txn.category_origin.value,
                txn.memo or None,
                "; ".join(links.get(txn.id, [])) or None,
            ]
        )
        for split in txn.splits:
            ws.append(
                [
                    "split",
                    txn.id,
                    split.id,
                    txn.posted_on,
                    label,
                    txn.payee,
                    dollars(split.amount_cents),
                    _category_name(names, split.category_id),
                    split.category_origin.value,
                    split.memo or None,
                    None,
                ]
            )
            ws.cell(row=ws.max_row, column=category_col).alignment = Alignment(indent=2)
    _format_column(ws, TRANSACTION_COLUMNS.index("date") + 1, DATE_FORMAT)
    _format_column(ws, TRANSACTION_COLUMNS.index("amount") + 1, MONEY_FORMAT)


def _category_sheet(ws: Worksheet, session: Session, period: Period, joint_ids: set[int]) -> None:
    _start_sheet(ws, CATEGORY_COLUMNS, CATEGORY_WIDTHS, freeze="A2")
    for line in by_category(session, period, joint_share=False):
        joint = "yes" if line.category_id in joint_ids else "no"
        ws.append([TYPE_LABELS[line.type], line.name, dollars(line.amount_cents), joint])
    totals = summary(session, period, joint_share=False).figures
    uncategorized = uncategorized_total(session, period, joint_share=False)
    ws.append([])
    for label, cents in (
        ("Total income", totals.income_cents),
        ("Total spending", totals.expenses_cents),
        ("Net", totals.net_cents),
        ("Uncategorized (net outflow)", uncategorized),
    ):
        ws.append([None, label, dollars(cents)])
    _format_column(ws, CATEGORY_COLUMNS.index("amount") + 1, MONEY_FORMAT)


def _link_summaries(
    session: Session, period: Period, transaction_ids: set[int]
) -> dict[int, list[str]]:
    """For each transaction in the period, its links as "kind ↔ #other", or just "kind" when
    the other side is not in the ledger, such as a receipt match."""
    in_period = select(Transaction.id).where(
        Transaction.posted_on >= period.start, Transaction.posted_on <= period.end
    )
    links = session.scalars(
        select(Link)
        .where(
            or_(
                Link.from_transaction_id.in_(in_period),
                Link.to_transaction_id.in_(in_period),
            )
        )
        .order_by(Link.id)
    ).all()
    summaries: dict[int, list[str]] = defaultdict(list)
    for link in links:
        for this, other in (
            (link.from_transaction_id, link.to_transaction_id),
            (link.to_transaction_id, link.from_transaction_id),
        ):
            if this is None or this not in transaction_ids:
                continue
            kind = link.kind.value
            summaries[this].append(kind if other is None else f"{kind} ↔ #{other}")
    return summaries


def _joint_category_ids(rows: Iterable[tuple[Transaction, Account]]) -> set[int]:
    """Categories with a transaction or split in a joint account this year."""
    ids: set[int] = set()
    for txn, account in rows:
        if not account.joint:
            continue
        if txn.splits:
            ids.update(split.category_id for split in txn.splits if split.category_id is not None)
        elif txn.category_id is not None:
            ids.add(txn.category_id)
    return ids


# Rental workbook


def _rental_summary_sheet(
    ws: Worksheet, properties: Sequence[_Property], by_account: dict[int, list[_Line]]
) -> None:
    _start_sheet(ws, SUMMARY_COLUMNS, SUMMARY_WIDTHS, freeze="A2")
    all_income = all_expenses = all_transfers = 0
    for prop in properties:
        income, expenses, transfers = _totals(by_account.get(prop.account_id, []))
        all_income += income
        all_expenses += expenses
        all_transfers += transfers
        ws.append(_summary_row(prop.label, prop.account, income, expenses, transfers))
    ws.append(_summary_row("All properties", None, all_income, all_expenses, all_transfers))
    for column in ("income", "expenses", "net", "transfers and loans (not in reports)"):
        _format_column(ws, SUMMARY_COLUMNS.index(column) + 1, MONEY_FORMAT)


def _summary_row(
    label: str, account: str | None, income: int, expenses: int, transfers: int
) -> list[object]:
    return [
        label,
        account,
        dollars(income),
        dollars(expenses),
        dollars(income + expenses),
        dollars(transfers),
    ]


def _property_sheet(ws: Worksheet, lines: Sequence[_Line], categories: Sequence[str]) -> None:
    columns = ("category", "type", *MONTHS, "Total")
    widths = (18, 12, *([11] * 12), 14)
    _start_sheet(ws, columns, widths, freeze="C2")
    counted = [line for line in lines if not line.excluded]
    grid = _monthly(counted)
    buckets = [*categories, OTHER]

    def months(bucket: str, direction: str) -> list[int]:
        return grid.get((bucket, direction), [0] * 12)

    for bucket in buckets:
        for direction in (INCOME, EXPENSES):
            values = months(bucket, direction)
            ws.append([bucket, direction, *(dollars(v) for v in values), dollars(sum(values))])

    income = [sum(months(bucket, INCOME)[m] for bucket in buckets) for m in range(12)]
    expenses = [sum(months(bucket, EXPENSES)[m] for bucket in buckets) for m in range(12)]
    net = [income[m] + expenses[m] for m in range(12)]
    for label, values in ((INCOME, income), (EXPENSES, expenses), ("Net", net)):
        ws.append(["Total", label, *(dollars(v) for v in values), dollars(sum(values))])
    transfers = _monthly_totals(line for line in lines if line.excluded)
    ws.append([TRANSFERS, "Net", *(dollars(v) for v in transfers), dollars(sum(transfers))])
    _format_range(ws, first_col=3, last_col=len(columns), number_format=MONEY_FORMAT)


def _rental_detail_sheet(
    ws: Worksheet, properties: Sequence[_Property], by_account: dict[int, list[_Line]]
) -> None:
    _start_sheet(ws, RENTAL_DETAIL_COLUMNS, RENTAL_DETAIL_WIDTHS, freeze="A2")
    for prop in properties:
        lines = sorted(
            by_account.get(prop.account_id, []),
            key=lambda line: (line.posted_on, line.transaction_id, line.split_id or 0),
        )
        for line in lines:
            ws.append(
                [
                    prop.label,
                    line.posted_on,
                    line.account,
                    line.payee,
                    line.memo,
                    line.transaction_id,
                    line.split_id,
                    dollars(line.cents),
                    line.category or UNCATEGORIZED,
                    line.bucket,
                ]
            )
    _format_column(ws, RENTAL_DETAIL_COLUMNS.index("date") + 1, DATE_FORMAT)
    _format_column(ws, RENTAL_DETAIL_COLUMNS.index("amount") + 1, MONEY_FORMAT)


def _rental_lines(
    rows: Iterable[tuple[Transaction, Account]],
    names: dict[int, str],
    categories: set[str],
    *,
    explained: dict[int, int],
    excluded_category_ids: set[int],
) -> list[_Line]:
    """The lines of each split, or of each unsplit transaction, counted as the reports count them.

    A transaction counts what its links leave unexplained (`explained` maps each transaction to
    the cents its links explain). A split shares that counted amount in proportion to its own
    amount, and the rest of the split is its explained part. A split's counted share goes to its
    rental category, and its explained part goes to Transfers, so the two lines add up to the split.
    A split in an excluded category is excluded in full. A fully explained split has no counted
    line. A split's memo falls back to its transaction's memo, because the rental detail has no
    parent row.
    """
    lines: list[_Line] = []
    for txn, account in rows:
        label = _account_label(account)
        counted = counted_amount(txn.amount_cents, explained.get(txn.id, 0), halve=False)
        if txn.splits:
            parts = [
                (split.id, split.amount_cents, split.category_id, split.memo or txn.memo)
                for split in txn.splits
            ]
            shares = counted_splits(txn.amount_cents, [part[1] for part in parts], counted)
        else:
            parts = [(None, txn.amount_cents, txn.category_id, txn.memo)]
            shares = [counted]
        for (split_id, cents, category_id, memo), share in zip(parts, shares, strict=True):
            category = None if category_id is None else names[category_id]
            if category_id in excluded_category_ids:
                pieces = [(cents, TRANSFERS, True)]
            else:
                pieces = []
                linked = cents - share
                if share or not linked:
                    bucket = category if category in categories else OTHER
                    pieces.append((share, bucket, False))
                if linked:
                    pieces.append((linked, TRANSFERS, True))
            for piece_cents, bucket, excluded in pieces:
                lines.append(
                    _Line(
                        account_id=account.id,
                        account=label,
                        transaction_id=txn.id,
                        split_id=split_id,
                        posted_on=txn.posted_on,
                        payee=txn.payee,
                        memo=memo or None,
                        cents=piece_cents,
                        category=category,
                        excluded=excluded,
                        bucket=bucket,
                    )
                )
    return lines


def _excluded_category_ids(session: Session) -> set[int]:
    """Categories that reports leave out: excluded and owed."""
    return set(session.scalars(select(Category.id).where(Category.type.in_(NOT_IN_REPORTS))))


def _monthly(lines: Iterable[_Line]) -> dict[tuple[str, str], list[int]]:
    """Counted cents per bucket and direction for each month, January first. A line of zero
    counts as income, and adds nothing either way."""
    grid: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * 12)
    for line in lines:
        direction = INCOME if line.cents >= 0 else EXPENSES
        grid[(line.bucket, direction)][line.posted_on.month - 1] += line.cents
    return dict(grid)


def _monthly_totals(lines: Iterable[_Line]) -> list[int]:
    """The cents of all these lines in each month of the year, January first."""
    months = [0] * 12
    for line in lines:
        months[line.posted_on.month - 1] += line.cents
    return months


def _totals(lines: Iterable[_Line]) -> tuple[int, int, int]:
    """Operating income, operating expenses, and the excluded transfers, in cents."""
    income = expenses = transfers = 0
    for line in lines:
        if line.excluded:
            transfers += line.cents
        elif line.cents >= 0:
            income += line.cents
        else:
            expenses += line.cents
    return income, expenses, transfers


def _properties(session: Session, accounts: dict[int, str]) -> list[_Property]:
    """The rental properties, sorted by label, then account ID."""
    found = {
        row.id: row
        for row in session.scalars(select(Account).where(Account.id.in_(list(accounts))))
    }
    ordered = sorted(accounts.items(), key=lambda item: (item[1].casefold(), item[0]))
    return [
        _Property(
            account_id=account_id,
            label=label,
            account=_account_label(found[account_id]) if account_id in found else "(missing)",
        )
        for account_id, label in ordered
    ]


def _sheet_title(prop: _Property, taken: set[str]) -> str:
    """A sheet name for a property: no characters Excel forbids, at most 31 long, and unique
    without regard to case. A repeated name gets a number, such as "Main (2)"."""
    cleaned = "".join(ch for ch in prop.label if ch not in "[]:*?/\\'").strip()
    cleaned = cleaned or f"Property {prop.account_id}"
    title = cleaned[:SHEET_TITLE_LIMIT]
    number = 2
    while title.casefold() in taken:
        suffix = f" ({number})"
        title = cleaned[: SHEET_TITLE_LIMIT - len(suffix)] + suffix
        number += 1
    taken.add(title.casefold())
    return title


# Shared helpers


def _year_period(year: int) -> Period:
    return Period(date(year, 1, 1), date(year, 12, 31))


def _destination(path: Path | None, exports_dir: Path, name: str) -> Path:
    """The file to write: the explicit path, or exports_dir/name. Creates its directory."""
    target = exports_dir / name if path is None else path
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _transactions(
    session: Session, period: Period, account_ids: Collection[int] | None = None
) -> list[tuple[Transaction, Account]]:
    """Transactions posted in the period, with their splits, oldest first, then by ID."""
    statement = (
        select(Transaction, Account)
        .join(Account, Account.id == Transaction.account_id)
        .options(selectinload(Transaction.splits))
        .where(Transaction.posted_on >= period.start, Transaction.posted_on <= period.end)
        .order_by(Transaction.posted_on, Transaction.id)
    )
    if account_ids is not None:
        statement = statement.where(Transaction.account_id.in_(list(account_ids)))
    return [(txn, account) for txn, account in session.execute(statement).all()]


def _category_names(session: Session) -> dict[int, str]:
    return dict(session.execute(select(Category.id, Category.name)).all())


def _category_name(names: dict[int, str], category_id: int | None) -> str:
    return UNCATEGORIZED if category_id is None else names[category_id]


def _account_label(account: Account) -> str:
    return account_digits(account.last4, account.alias_last4) or "••????"


def _start_sheet(
    ws: Worksheet, columns: Sequence[str], widths: Sequence[int], *, freeze: str
) -> None:
    """Write a bold, grey header row, set the column widths, and freeze the header."""
    ws.append(list(columns))
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.fill = _HEADER_FILL
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    ws.freeze_panes = freeze


def _format_column(ws: Worksheet, column: int, number_format: str) -> None:
    _format_range(ws, first_col=column, last_col=column, number_format=number_format)


def _format_range(ws: Worksheet, *, first_col: int, last_col: int, number_format: str) -> None:
    """Apply a number format to the data rows, below the header, for a range of columns."""
    last_row = ws.max_row
    for row in range(2, last_row + 1):
        for column in range(first_col, last_col + 1):
            ws.cell(row=row, column=column).number_format = number_format


__all__ = [
    "DATE_FORMAT",
    "MONEY_FORMAT",
    "NoRentalAccounts",
    "dollars",
    "export_rental",
    "export_transactions",
]
