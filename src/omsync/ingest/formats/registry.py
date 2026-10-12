"""Which statement formats to try for each institution, and in what order.

FORMATS maps an institution key to the formats to try, in order. The key "default" is used
for an institution that has no entry, and for no institution at all. "default" is reserved.
"""

from pathlib import Path

from omsync.ingest.formats.base import FormatError, StatementFormat, join_name_and_memo
from omsync.ingest.formats.boa_csv import BoaCsvFormat
from omsync.ingest.formats.ofx import OfxFormat

DEFAULT_KEY = "default"

FORMATS: dict[str, tuple[StatementFormat, ...]] = {
    # Citi changes FITIDs between downloads, so they only hint at a match.
    "citi": (OfxFormat(fitid_trusted=False),),
    # Chase gives a purchase and its foreign transaction fee one FITID, so a FITID is not an
    # identity there.
    # Chase also cuts NAME at 32 characters and continues the description in MEMO.
    "chase": (OfxFormat(fitid_trusted=False, memo_continues_name=True),),
    "chase_business": (OfxFormat(fitid_trusted=False, memo_continues_name=True),),
    # BoA's CSV comes first. Its OFX, if it ever arrives, is not trusted either.
    "boa": (BoaCsvFormat(), OfxFormat(fitid_trusted=False)),
    DEFAULT_KEY: (OfxFormat(fitid_trusted=True),),
}


def format_for(institution_key: str | None, path: Path) -> StatementFormat:
    """Return the first format for this institution whose detect() accepts the file."""
    candidates = FORMATS.get(institution_key or DEFAULT_KEY, FORMATS[DEFAULT_KEY])
    for statement_format in candidates:
        if statement_format.detect(path):
            return statement_format
    raise FormatError(f"{path.name}: no statement format matches this file")


def statement_description(
    institution_key: str | None, format_key: str, name: str, memo: str | None
) -> str:
    """The description of a statement row: the text that names the transaction.

    It is the NAME, with the MEMO joined on when the institution's format continues NAME there.
    Any other MEMO is a separate field, such as a reference number, and stays out. Import and
    reconciliation both read a row's description through this function.
    """
    candidates = FORMATS.get(institution_key or DEFAULT_KEY, FORMATS[DEFAULT_KEY])
    for statement_format in candidates:
        if (
            isinstance(statement_format, OfxFormat)
            and statement_format.key == format_key
            and statement_format.memo_continues_name
        ):
            return join_name_and_memo(name, memo)
    return name
