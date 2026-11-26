"""The vendor connector registry: keys and vendor connectors omsync knows.

register() adds a connector class when its module is imported, and keys must be unique.
Vendor connectors register when this module loads (see the bottom).
"""

from omsync.ingest.connectors.base import ConnectorError
from omsync.receipts.connectors.base import VendorConnector

_CONNECTORS: dict[str, VendorConnector] = {}


def register[C: type](cls: C) -> C:
    """Class decorator: instantiate the connector with no arguments and register it by its key."""
    connector = cls()
    if connector.key in _CONNECTORS:
        raise ValueError(f"vendor connector {connector.key!r} is already registered")
    _CONNECTORS[connector.key] = connector
    return cls


def get_connector(key: str) -> VendorConnector:
    """Return the registered vendor connector for a key. Raises ConnectorError if there is none."""
    connector = _CONNECTORS.get(key)
    if connector is None:
        raise ConnectorError(f"unknown vendor connector {key!r}")
    return connector


def find_connector(key: str) -> VendorConnector | None:
    """The registered vendor connector for a stored key, or None when no connector has it, such as
    one that was removed. A provider without one keeps its receipts and reads only the files you
    add to its folder."""
    return _CONNECTORS.get(key)


def registered_connectors() -> list[VendorConnector]:
    """Every registered vendor connector, sorted by key."""
    return [_CONNECTORS[key] for key in sorted(_CONNECTORS)]


# The vendor connectors register themselves when their modules load. This import runs after
# register() exists, so the vendor modules can import it without a cycle.
from omsync.receipts.connectors import amazon, costco, ebay  # noqa: E402, F401
