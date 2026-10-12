"""Imports every table model so Base.metadata is complete for Alembic, and re-exports them.

Batch tables are imported too: ledger foreign keys point at `batch`, so the metadata needs it.
"""

from omsync.core.batch_models import Batch, BatchChange, BatchState, DataRevision
from omsync.ledger.categories import AmountRule, Category, CategoryType, Keyword, SavedFilter
from omsync.ledger.links import (
    Acknowledgment,
    Counterparty,
    CounterpartyKind,
    Link,
    LinkKind,
    RejectedProposal,
    RejectionReason,
)
from omsync.ledger.plan_models import (
    BudgetKind,
    BudgetLine,
    Cadence,
    Goal,
    RecurringSeries,
    SeriesStatus,
)
from omsync.ledger.preferences import Preference
from omsync.ledger.sources import (
    Account,
    AccountAlias,
    AccountType,
    Institution,
    Provider,
    ProviderAccount,
)
from omsync.ledger.statements import BalanceKind, StatementBalance, StatementFile, StatementRow
from omsync.ledger.sync_models import SyncRun, SyncStatus
from omsync.ledger.transactions import (
    CategoryOrigin,
    ImportReview,
    MatchKind,
    ReviewStatus,
    Split,
    Transaction,
    TransactionRow,
)

__all__ = [
    "Acknowledgment",
    "Account",
    "AccountAlias",
    "AccountType",
    "AmountRule",
    "Batch",
    "BatchChange",
    "BatchState",
    "BalanceKind",
    "BudgetKind",
    "BudgetLine",
    "Cadence",
    "Category",
    "CategoryType",
    "CategoryOrigin",
    "Counterparty",
    "CounterpartyKind",
    "DataRevision",
    "Goal",
    "ImportReview",
    "Institution",
    "Keyword",
    "Link",
    "LinkKind",
    "MatchKind",
    "Preference",
    "Provider",
    "ProviderAccount",
    "RecurringSeries",
    "RejectedProposal",
    "RejectionReason",
    "ReviewStatus",
    "SavedFilter",
    "SeriesStatus",
    "Split",
    "StatementBalance",
    "StatementFile",
    "StatementRow",
    "SyncRun",
    "SyncStatus",
    "Transaction",
    "TransactionRow",
]
