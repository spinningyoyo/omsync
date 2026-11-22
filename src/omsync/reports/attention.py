"""What needs the person's attention today, as counts and the things they count.

Each item is counted by the query that the view it links to is built from, so the count and the
list behind it cannot disagree: import reviews by open_review_count, uncategorized transactions by
count_matching over UNCATEGORIZED (the filter the register opens with), reconciliation proposals
by the run the Reconcile page makes for the same range (what waits for a decision, not the
proposals applied on their own), missed occurrences by the Needs attention list of plan.recurring,
which the Recurring Overview shows, and sync problems by each provider's newest run. The web layer
turns the plain data into links.

Everything here reads and writes nothing, and nothing here imports omsync.web.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from omsync.ledger.bulk import count_matching
from omsync.ledger.filters import Condition, FilterSpec
from omsync.ledger.models import Institution, Provider, SyncRun, SyncStatus
from omsync.ledger.register import open_review_count
from omsync.plan.recurring import needs_attention
from omsync.reconcile.engine import run as run_reconciliation

# Uncategorized transactions: unsplit with no category, or split with a line that has none. The
# register opens with this filter, Triage lists the same transactions, and the count is taken over
# the same filter.
UNCATEGORIZED = FilterSpec(conditions=(Condition("category", "is_empty"),))
SYNC_PROBLEMS = (SyncStatus.failed, SyncStatus.needs_user_action)


@dataclass(frozen=True)
class ProviderIssue:
    """A provider whose newest sync run did not succeed and is not running."""

    key: str
    institution_name: str
    status: SyncStatus


@dataclass(frozen=True)
class Attention:
    """Every counted item. A count of 0 or an empty tuple means there is nothing of that kind."""

    open_reviews: int
    open_reconciliation: int
    uncategorized: int
    recurring_attention: int
    providers: tuple[ProviderIssue, ...]


def attention(session: Session, *, today: date, proposal_range: tuple[date, date]) -> Attention:
    """The counts as of `today`. Reconciliation items are not stored, so they are what a run over
    `proposal_range` finds waiting for a decision: review and manual proposals plus the transactions
    that are not matched, as the Reconcile page shows for that range. Automatic proposals are
    applied after each import, and transactions whose counterparty is known need nothing. Recurring
    items are the Needs attention list on the Recurring Overview: this calendar month's missed
    occurrences and open questions."""
    return Attention(
        open_reviews=open_review_count(session),
        open_reconciliation=run_reconciliation(
            session, start=proposal_range[0], end=proposal_range[1]
        ).needs_you,
        uncategorized=count_matching(session, UNCATEGORIZED),
        recurring_attention=recurring_attention_this_month(session, today=today),
        providers=problem_providers(session),
    )


def recurring_attention_this_month(session: Session, *, today: date) -> int:
    """The count of the Recurring Overview's Needs attention list: missed bills and open questions.
    Home and that list agree because both count the same items."""
    return len(needs_attention(session, today=today))


def problem_providers(session: Session) -> tuple[ProviderIssue, ...]:
    newest = select(func.max(SyncRun.id)).group_by(SyncRun.provider_id)
    rows = session.execute(
        select(Provider.key, Institution.name, SyncRun.status)
        .join(Institution, Institution.id == Provider.institution_id)
        .join(SyncRun, SyncRun.provider_id == Provider.id)
        .where(SyncRun.id.in_(newest), SyncRun.status.in_(SYNC_PROBLEMS))
        .order_by(Institution.name, Provider.key)
    ).all()
    return tuple(ProviderIssue(row.key, row.name, SyncStatus(row.status)) for row in rows)


__all__ = [
    "SYNC_PROBLEMS",
    "UNCATEGORIZED",
    "Attention",
    "ProviderIssue",
    "attention",
    "problem_providers",
    "recurring_attention_this_month",
]
