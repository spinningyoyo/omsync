"""The demo person's accounts and a year of their statement lines, written as statement files.

Kunchorn Omsin rents an apartment with a roommate, Sam Park, who pays half the rent by Zelle.
Kunchorn is paid every other Friday into a Bank of America checking account, saves into two savings
accounts, and spends on a Chase card and a Citi card that checking pays off each month. Kunchorn
lent a friend, Priya Nair, $300, and she has paid half of it back. There was a trip to Portland in
the summer, and Netflix raised its price this month. Last month Kunchorn moved $500 in by Zelle
from an account at a bank omsync does not track.

Every name, number, and amount is made up. The lines cover the twelve months before the month of
`today`, and that month up to yesterday, so the demo always looks current. One seed gives the same
year every time, shifted to the dates of the day it is built.
"""

import csv
import io
import random
import string
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from omsync.ledger.models import AccountType

MONTHS = 12
OWNER_INDN = "KUNCHORN OMSIN"


@dataclass(frozen=True)
class DemoAccount:
    key: str
    institution_key: str
    institution_name: str
    statement_account_id: str
    name: str
    type: AccountType
    opening_cents: int

    @property
    def last4(self) -> str:
        return self.statement_account_id[-4:]


CHECKING = DemoAccount(
    "checking", "boa", "Bank of America", "4821", "Everyday Checking", AccountType.checking, 420_000
)
EMERGENCY = DemoAccount(
    "emergency", "boa", "Bank of America", "9034", "Emergency Fund", AccountType.savings, 685_000
)
TRAVEL = DemoAccount(
    "travel", "boa", "Bank of America", "5560", "Travel Savings", AccountType.savings, 115_000
)
SAPPHIRE = DemoAccount(
    "sapphire", "chase", "Chase", "4147202655107702", "Sapphire Card", AccountType.credit_card, 0
)
DOUBLE_CASH = DemoAccount(
    "double_cash", "citi", "Citi", "5424180000003310", "Double Cash", AccountType.credit_card, 0
)
ACCOUNTS = (CHECKING, EMERGENCY, TRAVEL, SAPPHIRE, DOUBLE_CASH)


@dataclass(frozen=True)
class Line:
    """One statement line. Negative amounts left the account, or are charges on a card."""

    account: DemoAccount
    day: date
    amount_cents: int
    description: str


# (description, fewest and most per month, smallest and largest amount in cents)
_SAPPHIRE_SPENDING = (
    ("TRADER JOE'S #127", 3, 5, 2_800, 9_600),
    ("WHOLE FOODS MKT #10234", 1, 2, 4_000, 13_000),
    ("STARBUCKS STORE 05821", 4, 8, 465, 895),
    ("BLUE BOTTLE COFFEE", 2, 4, 550, 1_200),
    ("CHIPOTLE 2291", 1, 3, 1_100, 1_650),
    ("SWEETGREEN MISSION", 1, 3, 1_300, 1_850),
    ("DOORDASH*THAI BASIL", 1, 2, 2_400, 4_800),
    ("SQ *TARTINE BAKERY", 1, 2, 900, 2_400),
    ("UBER *TRIP", 2, 4, 900, 3_200),
    ("CHEVRON 0204381", 1, 2, 3_800, 6_200),
    ("TARGET 00012345", 0, 2, 1_800, 8_500),
    ("CVS/PHARMACY #09876", 0, 1, 800, 3_200),
    ("AMC METREON 16", 0, 1, 1_800, 3_600),
    ("REI #128 BERKELEY", 0, 1, 4_500, 16_000),
)
# (description, day of the month, amount in cents)
_SAPPHIRE_MONTHLY = (
    ("MISSION CLIFFS SF", 1, 9_500),
    ("APPLE.COM/BILL", 3, 299),
    ("SPOTIFY USA", 12, 1_199),
    ("T-MOBILE AUTOPAY", 18, 6_500),
    ("CLIPPER SERVICE", 2, 4_000),
)
_NETFLIX_DAY = 2
_NETFLIX_OLD, _NETFLIX_NEW = 1_549, 1_999
_NETFLIX_RAISE_MONTH = MONTHS  # the price goes up this month


def generate(today: date, seed: int = 20261025) -> list[Line]:
    """The demo year, as statement lines through the day before `today`, in date order."""
    rng = random.Random(seed)
    end = today - timedelta(days=1)
    first = _add_months(date(today.year, today.month, 1), -MONTHS)
    lines: list[Line] = []
    months = [_add_months(first, index) for index in range(MONTHS + 1)]
    for index, month in enumerate(months):
        lines.extend(_month(rng, index, month, end))
    lines.extend(_paychecks(first, end))
    lines.extend(_card_payments(lines, months, end))
    kept = [line for line in lines if first <= line.day <= end]
    return sorted(kept, key=lambda line: (line.day, line.account.key, -line.amount_cents))


def _month(rng: random.Random, index: int, month: date, end: date) -> list[Line]:
    lines: list[Line] = []

    def add(account: DemoAccount, day: int, cents: int, description: str) -> None:
        posted = _day_in(month, day)
        if posted <= end:
            lines.append(Line(account, posted, cents, description))

    # Card spending, spread over the month.
    for description, fewest, most, low, high in _SAPPHIRE_SPENDING:
        for _ in range(rng.randint(fewest, most)):
            add(SAPPHIRE, rng.randint(1, 28), -rng.randint(low, high), description)
    for _ in range(rng.randint(1, 3)):
        add(SAPPHIRE, rng.randint(1, 28), -rng.randint(1_200, 6_800), _amazon(rng))
    for description, day, cents in _SAPPHIRE_MONTHLY:
        add(SAPPHIRE, day, -cents, description)
    netflix = _NETFLIX_NEW if index >= _NETFLIX_RAISE_MONTH else _NETFLIX_OLD
    add(SAPPHIRE, _NETFLIX_DAY, -netflix, "NETFLIX.COM")
    add(DOUBLE_CASH, rng.randint(4, 10), -rng.randint(12_000, 26_000), "COSTCO WHSE #0144")
    if rng.random() < 0.6:
        add(DOUBLE_CASH, rng.randint(11, 27), -rng.randint(2_500, 14_000), "HOME DEPOT #6831")
    if month.month == 12:
        add(DOUBLE_CASH, 6, -rng.randint(3_000, 7_500), "ETSY.COM*HANDMADEGOODS")
        add(DOUBLE_CASH, 11, -rng.randint(4_000, 9_000), "ETSY.COM*PAPERANDINK")
        add(SAPPHIRE, 14, -rng.randint(6_000, 12_000), _amazon(rng))

    # Bills and savings from checking.
    add(CHECKING, 1, -240_000, _ach("MAPLE COURT APTS", "RENT", rng, "WEB"))
    add(CHECKING, 2, 120_000, _zelle("from", "SAM PARK", rng))
    add(CHECKING, 12, -_utilities(rng, month), _ach("PGANDE", "WEB ONLINE", rng, "WEB"))
    add(CHECKING, 20, -7_000, _ach("COMCAST CALIFORNIA", "XFINITY", rng, "WEB"))
    for savings, cents in ((EMERGENCY, 60_000), (TRAVEL, 30_000)):
        conf = _code(rng, string.digits, 10)
        add(
            CHECKING, 16, -cents, f"Online Banking transfer to {savings.last4} Confirmation# {conf}"
        )
        add(
            savings,
            16,
            cents,
            f"Online Banking transfer from CHK {CHECKING.last4} Confirmation# {conf}",
        )
    add(EMERGENCY, 28, rng.randint(36, 48), "Interest Earned")
    add(TRAVEL, 28, rng.randint(4, 9), "Interest Earned")
    if index % 2 == 1:
        when = _day_in(month, rng.randint(3, 25))
        add(
            CHECKING,
            when.day,
            -rng.choice((6_000, 10_000)),
            f"BKOFAMERICA ATM {when:%m/%d} #000004821 WITHDRWL 2000 MISSION ST",
        )

    # People.
    if index == MONTHS - 6:
        add(CHECKING, 9, -30_000, _zelle("to", "PRIYA NAIR", rng))
    if index == MONTHS - 3:
        add(CHECKING, 14, 15_000, _zelle("from", "PRIYA NAIR", rng))
    if index in (MONTHS - 10, MONTHS - 7, MONTHS - 2):
        add(
            CHECKING,
            rng.randint(5, 26),
            -rng.randint(1_600, 3_400),
            _zelle("to", "PRIYA NAIR", rng),
        )
    if index == MONTHS - 1:
        add(CHECKING, 6, 4_500, _zelle("from", "TAYLOR KIM", rng))
        # Kunchorn's own money from a bank omsync does not track: Reconcile lists it as not matched.
        add(CHECKING, 9, 50_000, _zelle("from", OWNER_INDN, rng))

    # The summer trip, paid by card and topped up from travel savings.
    if index == MONTHS - 4:
        add(DOUBLE_CASH, 3, -48_260, "UNITED 0162345678901")
        add(DOUBLE_CASH, 5, -64_000, "AIRBNB * HMXY12ZQ")
        add(DOUBLE_CASH, 18, -21_240, "HERTZ RENT-A-CAR")
        add(SAPPHIRE, 19, -3_820, "POWELL'S BOOKS")
        add(SAPPHIRE, 19, -1_450, "PIP'S ORIGINAL DOUGHNUTS")
        add(SAPPHIRE, 20, -6_840, "TASTY N DAUGHTERS")
        conf = _code(rng, string.digits, 10)
        add(
            TRAVEL,
            22,
            -100_000,
            f"Online Banking transfer to CHK {CHECKING.last4} Confirmation# {conf}",
        )
        add(
            CHECKING,
            22,
            100_000,
            f"Online Banking transfer from {TRAVEL.last4} Confirmation# {conf}",
        )

    # A return: the purchase and its refund.
    if index == MONTHS - 2:
        order = _amazon(rng)
        add(SAPPHIRE, 5, -5_499, order)
        add(SAPPHIRE, 19, 5_499, order)
    return lines


def _paychecks(first: date, end: date) -> list[Line]:
    day = first + timedelta(days=(4 - first.weekday()) % 7)  # the first Friday
    lines = []
    while day <= end:
        lines.append(
            Line(
                CHECKING,
                day,
                214_633,
                f"NORTHWIND LABS DES:PAYROLL ID:XXXXX58213 INDN:{OWNER_INDN} CO ID:XXXXX38421 PPD",
            )
        )
        day += timedelta(days=14)
    return lines


def _card_payments(lines: list[Line], months: list[date], end: date) -> list[Line]:
    """Each card's autopay: checking pays the card's net charges of the month before."""
    payments: list[Line] = []
    for card, day, issuer, credit in (
        (SAPPHIRE, 25, "CHASE", "AUTOMATIC PAYMENT - THANK"),
        (DOUBLE_CASH, 21, "CITI", "PAYMENT THANK YOU"),
    ):
        for previous, month in zip(months, months[1:], strict=False):
            owed = -sum(
                line.amount_cents
                for line in lines
                if line.account is card and _same_month(line.day, previous)
            )
            posted = _day_in(month, day)
            if owed <= 0 or posted > end:
                continue
            ident = f"XXXXX{posted:%m%d}{card.last4[-2:]}"
            payments.append(
                Line(
                    CHECKING,
                    posted,
                    -owed,
                    f"{issuer} CREDIT CRD DES:AUTOPAY ID:{ident} INDN:{OWNER_INDN} "
                    "CO ID:XXXXX75801 PPD",
                )
            )
            payments.append(Line(card, posted, owed, credit))
    return payments


def write_statements(statements_dir: Path, lines: list[Line], end: date) -> list[Path]:
    """Write one statement file per account and month through `end` into the drop folder, and
    return them.

    Bank of America accounts get BoA's CSV in statements/BOA/<last four>/. The cards get OFX in
    statements/CHASE/ and statements/CITI/, as their banks offer them.
    """
    written: list[Path] = []
    for account in ACCOUNTS:
        mine = [line for line in lines if line.account is account]
        balance = account.opening_cents
        for month in sorted({date(line.day.year, line.day.month, 1) for line in mine}):
            rows = [line for line in mine if _same_month(line.day, month)]
            period_end = min(_month_end(month), end)
            if account.institution_key == "boa":
                folder = statements_dir / "BOA" / account.statement_account_id
                path = folder / f"stmt_{month:%Y-%m}.csv"
                text = _boa_csv(month, period_end, balance, rows)
            else:
                folder = statements_dir / account.institution_key.upper()
                path = folder / f"{account.institution_key}_{account.last4}_{month:%Y-%m}.ofx"
                text = _card_ofx(account, month, period_end, balance, rows)
            folder.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            written.append(path)
            balance += sum(line.amount_cents for line in rows)
    return written


def _boa_csv(month: date, period_end: date, opening: int, rows: list[Line]) -> str:
    credits = sum(line.amount_cents for line in rows if line.amount_cents > 0)
    debits = sum(line.amount_cents for line in rows if line.amount_cents < 0)
    closing = opening + credits + debits
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["Description", "", "Summary Amt."])
    writer.writerow([f"Beginning balance as of {month:%m/%d/%Y}", "", _dollars(opening)])
    writer.writerow(["Total credits", "", _dollars(credits)])
    writer.writerow(["Total debits", "", _dollars(debits)])
    writer.writerow([f"Ending balance as of {period_end:%m/%d/%Y}", "", _dollars(closing)])
    writer.writerow([])
    writer.writerow(["Date", "Description", "Amount", "Running Bal."])
    writer.writerow(
        [f"{month:%m/%d/%Y}", f"Beginning balance as of {month:%m/%d/%Y}", "", _dollars(opening)]
    )
    running = opening
    for line in rows:
        running += line.amount_cents
        writer.writerow(
            [
                f"{line.day:%m/%d/%Y}",
                line.description,
                _amount(line.amount_cents),
                _dollars(running),
            ]
        )
    return out.getvalue()


def _card_ofx(
    account: DemoAccount, month: date, period_end: date, opening: int, rows: list[Line]
) -> str:
    closing = opening + sum(line.amount_cents for line in rows)
    transactions = []
    for number, line in enumerate(rows, start=1):
        kind = "CREDIT" if line.amount_cents > 0 else "DEBIT"
        transactions.append(
            "          <STMTTRN>\n"
            f"            <TRNTYPE>{kind}</TRNTYPE>\n"
            f"            <DTPOSTED>{line.day:%Y%m%d}120000.000[0:GMT]</DTPOSTED>\n"
            f"            <TRNAMT>{_amount(line.amount_cents, grouped=False)}</TRNAMT>\n"
            f"            <FITID>{line.day:%Y%m%d}{account.last4}{number:04d}</FITID>\n"
            f"            <NAME>{escape(line.description)}</NAME>\n"
            "          </STMTTRN>\n"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" '
        'NEWFILEUID="NONE"?>\n'
        "<OFX>\n"
        "  <SIGNONMSGSRSV2>\n"
        "    <SONRS>\n"
        "      <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>\n"
        f"      <DTSERVER>{period_end:%Y%m%d}235959.000[0:GMT]</DTSERVER>\n"
        "      <LANGUAGE>ENG</LANGUAGE>\n"
        f"      <FI><ORG>{escape(account.institution_name.upper())}</ORG><FID>1001</FID></FI>\n"
        "    </SONRS>\n"
        "  </SIGNONMSGSRSV2>\n"
        "  <CREDITCARDMSGSRSV1>\n"
        "    <CCSTMTTRNRS>\n"
        f"      <TRNUID>{month:%Y%m}</TRNUID>\n"
        "      <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>\n"
        "      <CCSTMTRS>\n"
        "        <CURDEF>USD</CURDEF>\n"
        "        <CCACCTFROM>\n"
        f"          <ACCTID>{account.statement_account_id}</ACCTID>\n"
        "        </CCACCTFROM>\n"
        "        <BANKTRANLIST>\n"
        f"          <DTSTART>{month:%Y%m%d}000000.000[0:GMT]</DTSTART>\n"
        f"          <DTEND>{period_end:%Y%m%d}235959.000[0:GMT]</DTEND>\n"
        f"{''.join(transactions)}"
        "        </BANKTRANLIST>\n"
        "        <LEDGERBAL>\n"
        f"          <BALAMT>{_amount(closing, grouped=False)}</BALAMT>\n"
        f"          <DTASOF>{period_end:%Y%m%d}235959.000[0:GMT]</DTASOF>\n"
        "        </LEDGERBAL>\n"
        "      </CCSTMTRS>\n"
        "    </CCSTMTTRNRS>\n"
        "  </CREDITCARDMSGSRSV1>\n"
        "</OFX>\n"
    )


def _ach(company: str, kind: str, rng: random.Random, suffix: str) -> str:
    ident = _code(rng, string.digits, 5)
    return (
        f"{company} DES:{kind} ID:XXXXX{ident} INDN:{OWNER_INDN} CO ID:XXXXX{ident[::-1]} {suffix}"
    )


def _zelle(direction: str, name: str, rng: random.Random) -> str:
    conf = _code(rng, string.ascii_lowercase + string.digits, 9)
    return f"Zelle payment {direction} {name} Conf# {conf}"


def _amazon(rng: random.Random) -> str:
    return f"AMZN Mktp US*{_code(rng, string.ascii_uppercase + string.digits, 9)}"


def _utilities(rng: random.Random, month: date) -> int:
    """Higher in winter, for heating."""
    winter = month.month in (11, 12, 1, 2, 3)
    return rng.randint(9_500, 14_200) if winter else rng.randint(5_800, 8_400)


def _code(rng: random.Random, alphabet: str, length: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(length))


def _dollars(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def _amount(cents: int, *, grouped: bool = True) -> str:
    sign = "-" if cents < 0 else ""
    value = f"{abs(cents) / 100:,.2f}" if grouped else f"{abs(cents) / 100:.2f}"
    return f"{sign}{value}"


def _add_months(day: date, months: int) -> date:
    total = day.year * 12 + day.month - 1 + months
    return date(total // 12, total % 12 + 1, 1)


def _month_end(month: date) -> date:
    return _add_months(month, 1) - timedelta(days=1)


def _day_in(month: date, day: int) -> date:
    return min(date(month.year, month.month, 1) + timedelta(days=day - 1), _month_end(month))


def _same_month(day: date, month: date) -> bool:
    return day.year == month.year and day.month == month.month
