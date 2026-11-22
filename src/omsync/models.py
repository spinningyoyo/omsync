"""Imports every package's tables, so Base.metadata is complete at each entry point."""

import omsync.core.batch_models
import omsync.ledger.models
import omsync.plan.models
import omsync.receipts.models  # noqa: F401
