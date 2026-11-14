"""The `omsync` command. Each module below registers its commands on an app from groups.py."""

import omsync.models  # noqa: F401 -- every table on Base.metadata
from omsync.cli import (  # noqa: F401 -- each module registers its commands
    accounts,
    backup,
    batches,
    categorize,
    exports,
    imports,
    providers,
    receipts,
    sync,
    system,
)
from omsync.cli.groups import app

__all__ = ["app"]
