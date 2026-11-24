"""Which statement formats to try for each institution, and in what order.

FORMATS maps an institution key to the formats to try, in order. The key "default" is used
for an institution that has no entry, and for no institution at all. "default" is reserved.
"""

from pathlib import Path

from omsync.ingest.formats.base import FormatError, StatementFormat
from omsync.ingest.formats.boa_csv import BoaCsvFormat
from omsync.ingest.formats.ofx import OfxFormat
from omsync.ingest.formats.wealthfront_csv import WealthfrontCsvFormat
from omsync.ledger.statements import NAME_CONTINUES_IN_MEMO

DEFAULT_KEY = "default"


def _ofx(institution_key: str, *, fitid_trusted: bool) -> OfxFormat:
    """The OFX format for an institution, told from the ledger's list whether its MEMO continues
    its NAME."""
    return OfxFormat(
        fitid_trusted=fitid_trusted,
        memo_continues_name=(institution_key, OfxFormat.key) in NAME_CONTINUES_IN_MEMO,
    )


FORMATS: dict[str, tuple[StatementFormat, ...]] = {
    # Citi changes FITIDs between downloads, so they only hint at a match.
    "citi": (OfxFormat(fitid_trusted=False),),
    # Chase gives a purchase and its foreign transaction fee one FITID, so a FITID is not an
    # identity there.
    # Chase also cuts NAME at 32 characters and continues the description in MEMO.
    "chase": (_ofx("chase", fitid_trusted=False),),
    "chase_business": (_ofx("chase_business", fitid_trusted=False),),
    # BoA's CSV comes first. Its OFX, if it ever arrives, is not trusted either.
    "boa": (BoaCsvFormat(), OfxFormat(fitid_trusted=False)),
    "wealthfront": (WealthfrontCsvFormat(),),
    DEFAULT_KEY: (OfxFormat(fitid_trusted=True),),
}


def format_for(institution_key: str | None, path: Path) -> StatementFormat:
    """Return the first format for this institution whose detect() accepts the file."""
    candidates = FORMATS.get(institution_key or DEFAULT_KEY, FORMATS[DEFAULT_KEY])
    for statement_format in candidates:
        if statement_format.detect(path):
            return statement_format
    raise FormatError(f"{path.name}: no statement format matches this file")
