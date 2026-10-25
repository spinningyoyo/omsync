"""Links that explain part of a transaction's amount, and the pairs you turned down.

"Reconciled" is never stored. It is worked out from the links.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from omsync.core.db import Base
from omsync.ledger.enums import string_enum


class LinkKind(StrEnum):
    transfer = "transfer"
    receipt = "receipt"
    refund = "refund"
    p2p = "p2p"
    joint_settlement = "joint_settlement"


class Link(Base):
    """A typed relationship that explains all or part of a transaction's amount.

    amount_cents is a positive magnitude: the cents of the transaction's absolute amount that the
    link explains. Signs belong to transactions and splits, never to links. Coverage sums these
    magnitudes, so a negative link would make a partly explained purchase look under-explained.

    The batch that inserted a link is recorded in batch_change, not on the link.
    """

    __tablename__ = "link"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_link_amount_positive"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[LinkKind] = mapped_column(string_enum(LinkKind))
    amount_cents: Mapped[int]
    from_transaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("transaction.id"), index=True
    )
    to_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transaction.id"), index=True)
    receipt_key: Mapped[str | None]
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RejectionReason(StrEnum):
    rejected = "rejected"
    unlinked = "unlinked"


class RejectedProposal(Base):
    """A pair you turned down, remembered so no proposal for it comes back.

    signature is the kind-free pair key (see omsync.reconcile.model.pair_signature). reason says
    how the pair was turned down: you rejected a proposal, or removed a link that existed. rule,
    kind, and evidence say what was proposed or linked; they are empty on rows from before they
    were recorded. A rule with several unlinked links stops being applied automatically.
    """

    __tablename__ = "rejected_proposal"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    signature: Mapped[str] = mapped_column(unique=True)
    rejected_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    rule: Mapped[str | None]
    kind: Mapped[LinkKind | None] = mapped_column(string_enum(LinkKind))
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reason: Mapped[RejectionReason] = mapped_column(
        string_enum(RejectionReason),
        default=RejectionReason.rejected,
        server_default=RejectionReason.rejected.value,
    )
