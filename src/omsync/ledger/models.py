"""Imports every table model so Base.metadata is complete for Alembic, and re-exports them.

Batch tables are imported too: ledger foreign keys point at `batch`, so the metadata needs it.
"""

from omsync.core.batch_models import Batch, BatchChange, BatchState, DataRevision
from omsync.ledger.categories import (
    Category,
    CategoryRule,
    CategoryType,
    Keyword,
    RuleOtherSide,
    SavedFilter,
)
from omsync.ledger.contacts import (
    AccountNickname,
    Contact,
    ContactAccount,
    ContactKind,
    ContactName,
    ContactPattern,
    ContactRole,
)
from omsync.ledger.links import Link, LinkKind, RejectedProposal, RejectionReason
from omsync.ledger.plan_models import (
    BudgetKind,
    BudgetLine,
    Cadence,
    Goal,
    RecurringPaidElsewhere,
    RecurringPrice,
    RecurringRejection,
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
    ContactOrigin,
    ImportReview,
    MatchKind,
    ReviewStatus,
    Split,
    Transaction,
    TransactionRow,
)

__all__ = [
    "Account",
    "AccountAlias",
    "AccountNickname",
    "AccountType",
    "Batch",
    "BatchChange",
    "BatchState",
    "BalanceKind",
    "BudgetKind",
    "BudgetLine",
    "Cadence",
    "Category",
    "CategoryRule",
    "CategoryType",
    "CategoryOrigin",
    "Contact",
    "ContactAccount",
    "ContactKind",
    "ContactName",
    "ContactOrigin",
    "ContactPattern",
    "ContactRole",
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
    "RecurringPaidElsewhere",
    "RecurringPrice",
    "RecurringRejection",
    "RecurringSeries",
    "RejectedProposal",
    "RejectionReason",
    "ReviewStatus",
    "RuleOtherSide",
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
