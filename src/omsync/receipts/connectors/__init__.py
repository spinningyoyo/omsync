"""Vendor receipt connectors: browser-driven acquisition for merchants."""

from omsync.receipts.connectors.amazon import AmazonConnector
from omsync.receipts.connectors.base import VendorConnector, VendorConnectorProtocol
from omsync.receipts.connectors.costco import CostcoConnector
from omsync.receipts.connectors.ebay import EbayConnector
from omsync.receipts.connectors.registry import get_connector, register, registered_connectors
from omsync.receipts.connectors.walmart import WalmartConnector

__all__ = [
    "AmazonConnector",
    "CostcoConnector",
    "EbayConnector",
    "VendorConnector",
    "VendorConnectorProtocol",
    "WalmartConnector",
    "get_connector",
    "register",
    "registered_connectors",
]
