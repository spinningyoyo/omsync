"""Text the Plan and Recurring pages show or accept: dates, months written YYYY-MM, and amounts as
a form field holds them."""

import re
from datetime import date

from omsync.core.months import FIRST_DAY, LAST_DAY

MONTH_RE = re.compile(r"(\d{4})-(\d{2})")


def money_input(cents: int) -> str:
    """An amount as a text field shows it: dollars and cents, with no symbol or separators."""
    sign = "-" if cents < 0 else ""
    dollars, remainder = divmod(abs(cents), 100)
    return f"{sign}{dollars}.{remainder:02d}"


def fmt_date(value: date) -> str:
    return f"{value:%b} {value.day}, {value.year}"


def month_key(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def month_title(value: date) -> str:
    return f"{value:%B %Y}"


def parse_month(text: str) -> date | None:
    """The first day of a month written YYYY-MM, or None when the text is not a month."""
    match = MONTH_RE.fullmatch(text)
    if match is None:
        return None
    year, month = int(match[1]), int(match[2])
    if not (FIRST_DAY.year <= year <= LAST_DAY.year and 1 <= month <= 12):
        return None
    return date(year, month, 1)
