"""Triage's view of receipts: the items that hold up a charge of yours, and where they come from.

Only matched charges count. A receipt paid elsewhere, or one that matches no transaction, would
not apply with a keyword either, so its items are not listed.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.settings import Settings
from omsync.ledger.models import Account, Category, CategoryOrigin, Split, Transaction
from omsync.receipts.apply import keyword_categories
from omsync.receipts.match import MatchOutcome
from omsync.receipts.model import ReceiptItem
from omsync.receipts.service import run_vendor
from omsync.receipts.vendors import get_vendor

CategoryName = Callable[[ReceiptItem], str | None]


@dataclass(frozen=True)
class SourceLine:
    """One line of a receipt: an item, or another payment such as a reward. category is the name
    the keywords give it now. None means it waits for a keyword."""

    description: str
    amount_cents: int
    category: str | None


@dataclass(frozen=True)
class ItemSource:
    """A receipt that a waiting item is on, and the transaction its charge matched."""

    receipt_id: str
    account: str
    purchased_on: date
    is_refund: bool
    url: str | None
    lines: tuple[SourceLine, ...]
    transaction_id: int
    account_id: int
    account_name: str
    posted_on: date
    amount_cents: int
    category: str | None
    can_keep: bool  # a whole category that a keyword or a rule gave it


@dataclass(frozen=True)
class WaitingItem:
    """Text that no keyword matches, and the receipts it holds up."""

    text: str
    sources: tuple[ItemSource, ...]


def has_receipt_files(settings: Settings, vendor_key: str) -> bool:
    """True when a provider folder of the vendor holds at least one file the vendor can discover."""
    vendor = get_vendor(vendor_key)
    root = settings.receipts_dir / vendor.folder
    if not root.is_dir():
        return False
    return any(vendor.discover(folder) for folder in root.iterdir() if folder.is_dir())


def waiting_items(
    session: Session, settings: Settings, vendor_key: str, *, today: date
) -> list[WaitingItem]:
    """The items and other payments that hold up a matched charge because nothing gives them a
    category, grouped by their text, ignoring case and extra spaces. The item on the most receipts
    comes first."""
    report = run_vendor(session, settings, vendor_key, today=today)
    if report.match is None:
        return []
    vendor = get_vendor(vendor_key)
    category_for = _category_names(session, vendor_key)
    outcomes = {outcome.receipt_key: outcome for outcome in report.match.outcomes}
    grouped: dict[str, list[ItemSource]] = {}
    display: dict[str, str] = {}
    for skip in report.plan.skipped:
        if not (skip.item_positions or skip.offset_names):
            continue
        outcome = outcomes[skip.receipt_key]
        source = _source(
            session, vendor.receipt_url(outcome.receipt.receipt_id), outcome, category_for
        )
        if source is None:
            continue
        names = [
            item.description
            for item in outcome.receipt.items
            if item.position in skip.item_positions
        ] + list(skip.offset_names)
        for name in names:
            text = " ".join(name.split())
            key = text.casefold()
            display.setdefault(key, text)
            group = grouped.setdefault(key, [])
            if source not in group:
                group.append(source)
    ranked = sorted(grouped, key=lambda key: (-len(grouped[key]), display[key].casefold()))
    return [WaitingItem(display[key], tuple(grouped[key])) for key in ranked]


@dataclass(frozen=True)
class ReceiptDetail:
    """A receipt and its lines, with the transactions its charges matched, if any. total_cents has
    the sign of the lines, so a refund's total is what came back."""

    receipt_id: str
    account: str
    purchased_on: date
    is_refund: bool
    total_cents: int
    lines: tuple[SourceLine, ...]
    sources: tuple[ItemSource, ...]

    @property
    def tax_cents(self) -> int:
        """What the total holds beyond the lines: the tax, and any other amount."""
        return self.total_cents - sum(line.amount_cents for line in self.lines)


def receipt_detail(
    session: Session, settings: Settings, vendor_key: str, receipt_id: str, *, today: date
) -> ReceiptDetail | None:
    """One receipt from the vendor's files, or None when no file has it."""
    report = run_vendor(session, settings, vendor_key, today=today)
    if report.match is None:
        return None
    outcomes = [o for o in report.match.outcomes if o.receipt.receipt_id == receipt_id]
    if not outcomes:
        return None
    vendor = get_vendor(vendor_key)
    category_for = _category_names(session, vendor_key)
    receipt = outcomes[0].receipt
    sources: list[ItemSource] = []
    for outcome in outcomes:
        source = _source(session, vendor.receipt_url(receipt_id), outcome, category_for)
        if source is not None and source not in sources:
            sources.append(source)
    offsets = [
        ReceiptItem(0, offset.description, offset.amount_cents, taxable=False)
        for outcome in outcomes[:1]
        if outcome.charge is not None
        for offset in outcome.charge.offsets
    ]
    return ReceiptDetail(
        receipt_id=receipt_id,
        account=receipt.account,
        purchased_on=receipt.purchased_on,
        is_refund=receipt.is_refund,
        # A refund's lines are positive, as the money that comes back, and its total negative.
        total_cents=-receipt.total_cents if receipt.is_refund else receipt.total_cents,
        lines=tuple(
            SourceLine(item.description, item.amount_cents, category_for(item))
            for item in [*receipt.items, *offsets]
        ),
        sources=tuple(sources),
    )


def _category_names(session: Session, vendor_key: str) -> CategoryName:
    category_id_for = keyword_categories(session, vendor_key)
    names = dict(session.execute(select(Category.id, Category.name)).all())

    def name_for(item: ReceiptItem) -> str | None:
        category_id = category_id_for(item)
        return None if category_id is None else names.get(category_id)

    return name_for


def _source(
    session: Session, url: str | None, outcome: MatchOutcome, category_for: CategoryName
) -> ItemSource | None:
    charge = outcome.charge
    txn = (
        None if outcome.transaction_id is None else session.get(Transaction, outcome.transaction_id)
    )
    if txn is None or charge is None:
        return None
    account = session.get(Account, txn.account_id)
    category = None if txn.category_id is None else session.get(Category, txn.category_id)
    split = session.scalar(select(Split.id).where(Split.transaction_id == txn.id).limit(1))
    receipt = outcome.receipt
    others = [
        ReceiptItem(0, offset.description, offset.amount_cents, taxable=False)
        for offset in charge.offsets
    ]
    lines = tuple(
        SourceLine(item.description, item.amount_cents, category_for(item))
        for item in [*receipt.items, *others]
    )
    return ItemSource(
        receipt_id=receipt.receipt_id,
        account=receipt.account,
        purchased_on=receipt.purchased_on,
        is_refund=receipt.is_refund,
        url=url,
        lines=lines,
        transaction_id=txn.id,
        account_id=txn.account_id,
        account_name=account.name if account is not None else "",
        posted_on=txn.posted_on,
        amount_cents=txn.amount_cents,
        category=None if category is None else category.name,
        can_keep=category is not None
        and split is None
        and txn.category_origin != CategoryOrigin.manual.value,
    )
