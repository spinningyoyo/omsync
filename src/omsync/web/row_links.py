"""What the register's link menu shows for a transaction: one entry per link, each opening what the
link ties the transaction to."""

from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

from omsync.core.money import format_cents
from omsync.ledger.filters import Condition, FilterSpec
from omsync.ledger.links import LinkKind
from omsync.ledger.register import RowLink
from omsync.receipts.vendors import get_vendor
from omsync.web.register_state import query_string

LINK_ICONS = {
    LinkKind.transfer: "arrow-left-right",
    LinkKind.receipt: "receipt",
    LinkKind.refund: "undo-2",
    LinkKind.p2p: "users",
    LinkKind.joint_settlement: "credit-card",
}


@dataclass(frozen=True)
class LinkItem:
    text: str
    icon: str
    href: str | None
    external: bool = False


def transaction_href(account_id: int, posted_on: date, amount_cents: int) -> str:
    """Transactions, showing one transaction by its account, date, and amount."""
    spec = FilterSpec((Condition("date", "is", posted_on), Condition("amount", "is", amount_cents)))
    return "/transactions?" + query_string(account_id, spec, None)


def pair_href(*transaction_ids: int) -> str:
    """Transactions, showing the transactions of a link together."""
    spec = FilterSpec((Condition("transaction", "is", sorted(transaction_ids)),))
    return "/transactions?" + query_string(None, spec, None)


def link_items(transaction_id: int, links: list[RowLink]) -> list[LinkItem]:
    """One entry per link. A link to another transaction opens the pair, and a receipt opens the
    vendor's page for it, or omsync's own when the vendor has none."""
    items: list[LinkItem] = []
    for link in links:
        icon = LINK_ICONS[link.kind]
        if link.other is not None:
            other = link.other
            account = f"{other.account_name} {other.account_digits}".strip()
            text = (
                f"{link.label} · {account} · {other.posted_on.isoformat()} · "
                f"{format_cents(other.amount_cents)}"
            )
            items.append(LinkItem(text, icon, pair_href(transaction_id, other.id)))
        elif link.receipt_key is not None:
            items.append(_receipt_item(link.receipt_key, icon))
    return items


def receipt_href(vendor_key: str, receipt_id: str) -> str:
    """The vendor's own page for a receipt, or omsync's page for it when the vendor has none."""
    url = get_vendor(vendor_key).receipt_url(receipt_id)
    return url or f"/receipts/{quote(vendor_key)}/{quote(receipt_id)}"


def _receipt_item(receipt_ref: str, icon: str) -> LinkItem:
    vendor_key, _, receipt_id = receipt_ref.partition(":")
    try:
        vendor = get_vendor(vendor_key)
    except KeyError:
        return LinkItem(f"Receipt {receipt_ref}", icon, None)
    url = vendor.receipt_url(receipt_id)
    if url is None:
        return LinkItem(
            vendor.receipt_label(receipt_id),
            icon,
            f"/receipts/{quote(vendor_key)}/{quote(receipt_id)}",
        )
    return LinkItem(vendor.receipt_label(receipt_id), icon, url, external=True)
