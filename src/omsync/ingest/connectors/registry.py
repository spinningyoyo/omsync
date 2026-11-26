"""The connector registry: the connector keys omsync knows, the built-in drop folder, and the banks.

register() adds a connector class when its module is imported, and keys must be unique.
The bank connectors in connectors/banks/ register when this module loads (see the bottom).
Tests that need a connector of their own replace the table for the length of the test,
rather than registering one for the whole process.
"""

from pathlib import Path

from omsync.core.vault import Vault
from omsync.ingest.connectors.base import (
    Connector,
    ConnectorAccount,
    ConnectorError,
    DateRange,
    Prompt,
)

_CONNECTORS: dict[str, Connector] = {}


def register[C: type](cls: C) -> C:
    """Class decorator: instantiate the connector with no arguments and register it by its key."""
    connector = cls()
    if connector.key in _CONNECTORS:
        raise ValueError(f"connector {connector.key!r} is already registered")
    _CONNECTORS[connector.key] = connector
    return cls


def get_connector(key: str) -> Connector:
    """Return the registered connector for a key. Raises ConnectorError if there is none."""
    connector = _CONNECTORS.get(key)
    if connector is None:
        raise ConnectorError(f"unknown connector {key!r}")
    return connector


def find_connector(key: str | None) -> Connector | None:
    """The registered connector for a stored key, or None when no connector has it, such as one
    that was removed. A provider without one keeps its accounts and files, and fetches nothing."""
    return _CONNECTORS.get(key) if key else None


def fetching_connector(key: str | None) -> Connector:
    """The connector a provider fetches with: its own, or the drop folder when it has none."""
    return find_connector(key) or _CONNECTORS[DropFolder.key]


def registered_connectors() -> list[Connector]:
    """Every registered connector, sorted by key."""
    return [_CONNECTORS[key] for key in sorted(_CONNECTORS)]


@register
class DropFolder:
    """Statement files are placed in the drop folder by hand, so there is nothing to fetch."""

    key = "drop_folder"
    label = "Drop folder: files you add by hand"
    last4_is_statement_id = False

    def fetch(
        self,
        accounts: list[ConnectorAccount],
        period: DateRange,
        vault: Vault | None,
        prompt: Prompt,
        dest: Path,
        *,
        vault_item_id: str | None,
    ) -> list[Path]:
        return []


# The bank connectors register themselves when their modules load. This import runs after
# register() exists, so the bank modules can import it without a cycle.
from omsync.ingest.connectors.banks import (  # noqa: E402, F401
    boa,
    chase,
    citi,
    fidelity,
    wealthfront,
)
