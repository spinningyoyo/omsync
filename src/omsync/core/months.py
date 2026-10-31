"""Calendar-month arithmetic shared by planning, reports, and receipt fetching."""

import calendar
from datetime import date

# The dates a person can enter. Periods compare with the one before and reconciliation looks a few
# days either side, so a typed date stays well inside the calendar's years 1 to 9999.
FIRST_DAY = date(1900, 1, 1)
LAST_DAY = date(2199, 12, 31)


def parse_day(text: str) -> date:
    """A date written YYYY-MM-DD between FIRST_DAY and LAST_DAY. Anything else is a ValueError."""
    day = date.fromisoformat(text.strip())
    if not FIRST_DAY <= day <= LAST_DAY:
        raise ValueError(f"date out of range: {day}")
    return day


def shift_month_start(day: date, months: int) -> date:
    """The first day of the month `months` away from the month of `day`. Negative goes back."""
    year, month_index = divmod(day.year * 12 + (day.month - 1) + months, 12)
    return date(year, month_index + 1, 1)


def shift_months(day: date, months: int, *, anchor_day: int | None = None) -> date:
    """`day` moved by whole calendar months. The result lands on `anchor_day`, or on the day of
    `day` when there is no anchor, and a day past the end of the month becomes its last day:
    January 31 plus one month is February 28, or 29 in a leap year. An anchor on the 31st returns
    to the 31st in March, where stepping on from February 28 without one would not."""
    year, month_index = divmod(day.year * 12 + (day.month - 1) + months, 12)
    month = month_index + 1
    wanted = day.day if anchor_day is None else anchor_day
    return date(year, month, min(wanted, calendar.monthrange(year, month)[1]))


def month_end(day: date) -> date:
    """The last day of the month of `day`."""
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])
