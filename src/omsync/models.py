"""Imports every package's tables, so Base.metadata is complete at each entry point."""

import omsync.core.batch_models
import omsync.ingest.models
import omsync.ledger.models
import omsync.plan.models  # noqa: F401
