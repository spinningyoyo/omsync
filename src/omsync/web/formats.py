"""Text the web pages show for dates and amounts, and the months the Plan and Recurring pages
accept as YYYY-MM. Amounts are integer cents; see omsync.core.money."""

import re
from datetime import date

from omsync.core.money import format_cents
from omsync.core.months import FIRST_DAY, LAST_DAY

MONTH_RE = re.compile(r"(\d{4})-(\d{2})")
EM_DASH = "—"


def money_input(cents: int) -> str:
    """An amount as a text field shows it: dollars and cents, with no symbol or separators."""
    sign = "-" if cents < 0 else ""
    dollars, remainder = divmod(abs(cents), 100)
    return f"{sign}{dollars}.{remainder:02d}"


def money_or_dash(cents: int | None) -> str:
    """An amount, or an em dash when there is none. Privacy mode blurs the figures in CSS."""
    return EM_DASH if cents is None else format_cents(cents)


def long_day(day: date) -> str:
    """A date such as Jan 5, 2026. Unlike fmt_date, %Y pads a year before 1000."""
    return f"{day:%b} {day.day}, {day:%Y}"


def short_day(day: date) -> str:
    """A date with its weekday and no year, such as Mon Jan 5."""
    return f"{day:%a %b} {day.day}"


def iso_day(day: date) -> str:
    return day.isoformat()


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
