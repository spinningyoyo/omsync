"""Costco warehouse receipt acquisition through an authenticated Chrome session.

This module delegates to the Costco vendor connector in omsync.receipts.connectors.
"""

from datetime import date
from pathlib import Path

from omsync.core.settings import Settings
from omsync.ingest.connectors.base import Prompt
from omsync.receipts.connectors import get_connector
from omsync.receipts.connectors.costco import (
    ACCOUNT_HOME_URL,
    BROWSER_MODE,
    CHUNK_MONTHS,
    CLIENT_IDENTIFIER,
    GRAPHQL_TIMEOUT,
    GRAPHQL_URL,
    RECEIPT_DETAIL_QUERY,
    RECEIPTS_QUERY,
    RECEIPTS_SCRIPT,
    RECEIPTS_SUMMARY_QUERY,
    SESSION_MISSING,
    SIGN_IN_NOT_DONE,
    SIGN_IN_QUESTION,
    SIGN_IN_TIMED_OUT,
    STORAGE_SCRIPT,
    quarter_ranges,
)


def fetch_receipts(settings: Settings, prompt: Prompt, *, start: date, end: date) -> list[Path]:
    """Save each Costco receipt from start to end (inclusive) that is not on disk yet.

    The browser opens the account page. You sign in there and press Enter at the prompt. Returns
    the files this call wrote. Raises NeedsUserAction when the sign-in or the session needs you, and
    ConnectorError for anything else, such as a Chrome that is not listening for debugging. No
    message holds a token or the membership number.
    """
    connector = get_connector("costco")
    return connector.fetch(settings, prompt, start=start, end=end)


__all__ = [
    "ACCOUNT_HOME_URL",
    "BROWSER_MODE",
    "CHUNK_MONTHS",
    "CLIENT_IDENTIFIER",
    "GRAPHQL_TIMEOUT",
    "GRAPHQL_URL",
    "RECEIPTS_QUERY",
    "RECEIPTS_SCRIPT",
    "RECEIPTS_SUMMARY_QUERY",
    "RECEIPT_DETAIL_QUERY",
    "SESSION_MISSING",
    "SIGN_IN_NOT_DONE",
    "SIGN_IN_QUESTION",
    "SIGN_IN_TIMED_OUT",
    "STORAGE_SCRIPT",
    "fetch_receipts",
    "quarter_ranges",
]
