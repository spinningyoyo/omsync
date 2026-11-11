"""Build the demo data directory: Kunchorn Omsin's year of money, made up and imported as real data.

Everything is written through the same service functions the app uses, each as a batch with an
idempotency key, and the statement files go through the same import the drop folder uses. So the
demo's categories, links, and balance checks are the app's own work, not rows put there by hand.
"""

from datetime import date, timedelta
from pathlib import Path

from alembic import command
from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.db import database_url, make_engine
from omsync.core.schema import alembic_config
from omsync.core.settings import Settings
from omsync.demo import MARKER_NAME, statements
from omsync.ingest.accounts import create_account
from omsync.ingest.dropfolder import pending_files
from omsync.ingest.pipeline import ImportReport, import_files
from omsync.ledger.categories_admin import (
    RuleDraft,
    add_keyword,
    add_rule,
    create_category,
    update_category,
)
from omsync.ledger.categorize import PAYEE_LIST
from omsync.ledger.contacts import ContactKind, ContactRole, PersonName, create_contact
from omsync.ledger.payment_methods import PaymentMethod
from omsync.ledger.plan_models import BudgetKind, RecurringSeries, SeriesStatus
from omsync.ledger.sources import Account
from omsync.plan.budgets import set_line
from omsync.plan.goals import create_goal
from omsync.plan.recurring import confirm, detect, dismiss, update_series

# Recurring suggestions by the start of their name, with the name the demo gives the confirmed
# series. The demo confirms the bills, pay, rent share, savings transfers, and card autopays; leaves
# two in the review queue on purpose, so Recurring has them to show; and dismisses the rest, which
# are everyday shopping that happens to repeat, or the other half of a transfer already confirmed.
CONFIRMED = {
    "NORTHWIND LABS": "Northwind Labs pay",
    "MAPLE COURT APTS": "Rent",
    "Zelle payment from SAM PARK": "Sam's rent share",
    "PGANDE": "PG&E",
    "COMCAST": "Xfinity internet",
    "T-MOBILE": "T-Mobile",
    "NETFLIX": "Netflix",
    "SPOTIFY": "Spotify",
    "MISSION CLIFFS": "Mission Cliffs",
    "Online Banking transfer to 9034": "To Emergency Fund",
    "Online Banking transfer to 5560": "To Travel Savings",
    "CHASE CREDIT CRD": "Sapphire Card autopay",
    "CITI CREDIT CRD": "Double Cash autopay",
}
HELD_FOR_REVIEW = ("APPLE.COM", "CLIPPER")

# (name, type, Lucide icon)
CATEGORIES = (
    ("Paycheck", "income", "banknote"),
    ("Interest", "income", "percent"),
    ("Rent", "fixed", "house"),
    ("Utilities", "fixed", "zap"),
    ("Phone & internet", "fixed", "wifi"),
    ("Subscriptions", "fixed", "tv"),
    ("Fitness", "fixed", "dumbbell"),
    ("Groceries", "flexible", "shopping-cart"),
    ("Dining", "flexible", "utensils"),
    ("Coffee", "flexible", "coffee"),
    ("Transport", "flexible", "car"),
    ("Shopping", "flexible", "shopping-bag"),
    ("Entertainment", "flexible", "ticket"),
    ("Health", "flexible", "heart-pulse"),
    ("Cash", "flexible", "wallet"),
    ("Travel", "non_monthly", "plane"),
    ("Gifts", "non_monthly", "gift"),
    ("Home", "non_monthly", "hammer"),
    ("Transfer", "excluded", "arrow-left-right"),
    ("Credit card payment", "excluded", "credit-card"),
    ("Loans", "owed", "hand-coins"),
)

# (pattern, category). Payee keywords are literal and case-insensitive.
PAYEE_KEYWORDS = (
    ("NORTHWIND LABS", "Paycheck"),
    ("Interest Earned", "Interest"),
    ("MAPLE COURT APTS", "Rent"),
    ("PGANDE", "Utilities"),
    ("COMCAST", "Phone & internet"),
    ("T-MOBILE", "Phone & internet"),
    ("NETFLIX", "Subscriptions"),
    ("SPOTIFY", "Subscriptions"),
    ("APPLE.COM/BILL", "Subscriptions"),
    ("MISSION CLIFFS", "Fitness"),
    ("TRADER JOE", "Groceries"),
    ("WHOLE FOODS", "Groceries"),
    ("COSTCO", "Groceries"),
    ("CHIPOTLE", "Dining"),
    ("SWEETGREEN", "Dining"),
    ("DOORDASH", "Dining"),
    ("TASTY N DAUGHTERS", "Dining"),
    ("PIP'S ORIGINAL", "Dining"),
    ("STARBUCKS", "Coffee"),
    ("UBER", "Transport"),
    ("CLIPPER", "Transport"),
    ("CHEVRON", "Transport"),
    ("TARGET", "Shopping"),
    ("AMZN MKTP", "Shopping"),
    ("POWELL'S", "Shopping"),
    ("AMC", "Entertainment"),
    ("CVS/PHARMACY", "Health"),
    ("BKOFAMERICA ATM", "Cash"),
    ("UNITED 0", "Travel"),
    ("AIRBNB", "Travel"),
    ("HERTZ", "Travel"),
    ("ETSY", "Gifts"),
    ("HOME DEPOT", "Home"),
)

# (pattern, category), regular expressions. Zelle and bank transfers need no keyword: the
# rules below read how the money moved.
REGEX_KEYWORDS = (
    ("CREDIT CRD DES:AUTOPAY", "Credit card payment"),
    (r"PAYMENT\s*-?\s*THANK", "Credit card payment"),
)

# Budgets, in cents a month, for the current month and the two before it.
BUDGETS = (
    ("Rent", 120_000),
    ("Utilities", 12_000),
    ("Phone & internet", 13_500),
    ("Subscriptions", 3_500),
    ("Fitness", 9_500),
    ("Groceries", 65_000),
    ("Dining", 30_000),
    ("Coffee", 7_000),
    ("Transport", 18_000),
    ("Shopping", 25_000),
    ("Entertainment", 6_000),
    ("Health", 4_000),
    ("Cash", 8_000),
    ("Travel", 30_000),
    ("Gifts", 7_500),
    ("Home", 10_000),
)


def build_demo(data_dir: Path, *, today: date) -> None:
    """Create the demo in data_dir, which must be new or empty, and import a year of statements.

    Raises ValueError for a directory that already holds files, and RuntimeError when an import
    fails or a balance check does not agree, so a broken demo is never served.
    """
    if data_dir.exists() and any(data_dir.iterdir()):
        raise ValueError(f"{data_dir} is not empty; the demo needs a new or empty directory")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / MARKER_NAME).write_text("Built by omsync's demo. Made-up data.\n", encoding="utf-8")

    settings = Settings(data_dir=data_dir)
    command.upgrade(alembic_config(database_url(settings)), "head")
    engine = make_engine(database_url(settings))
    try:
        with Session(engine) as session:
            contacts = _people(session)
            accounts = _accounts(session)
            categories = _categories(session)
            _keywords(session, categories)
            _rules(session, categories, contacts)
            statements.write_statements(
                settings.statements_dir,
                statements.generate(today),
                today - timedelta(days=1),
            )
            # Recurring is decided before this month's statements arrive, as it would have been,
            # so this month's higher Netflix charge asks "New amount?" instead of being learned.
            this_month = f"{today:%Y-%m}"
            files = pending_files(session, settings)
            earlier = [item for item in files if this_month not in item.path.name]
            _check_import(import_files(session, settings, earlier, apply=True))
            _recurring(session, today - timedelta(days=today.day))
            _check_import(
                import_files(session, settings, pending_files(session, settings), apply=True)
            )
            _budgets(session, categories, today)
            _goals(session, accounts, today)
    finally:
        engine.dispose()


def _people(session: Session) -> dict[str, int]:
    """The owner and the two contacts the demo's rules and balances read, by first name."""
    create_contact(
        session,
        name="Kunchorn Omsin",
        kind=ContactKind.person,
        role=ContactRole.owner,
        names=[PersonName("Kunchorn", family="Omsin")],
        idempotency_key="demo:owner",
    )
    ids: dict[str, int] = {}
    for given, family in (("Sam", "Park"), ("Priya", "Nair")):
        batch = create_contact(
            session,
            name=f"{given} {family}",
            kind=ContactKind.person,
            names=[PersonName(given, family=family)],
            idempotency_key=f"demo:contact:{given.lower()}",
        )
        ids[given] = int(batch.summary["contact_id"])
    return ids


def _accounts(session: Session) -> dict[str, Account]:
    """The demo's accounts, by their key in statements.ACCOUNTS."""
    for account in statements.ACCOUNTS:
        create_account(
            session,
            institution_key=account.institution_key,
            institution_name=account.institution_name,
            statement_account_id=account.statement_account_id,
            name=account.name,
            account_type=account.type,
            last4=account.last4,
            idempotency_key=f"demo:account:{account.key}",
        )
    return {
        account.key: session.scalars(select(Account).where(Account.name == account.name)).one()
        for account in statements.ACCOUNTS
    }


def _categories(session: Session) -> dict[str, int]:
    ids: dict[str, int] = {}
    for name, category_type, icon in CATEGORIES:
        batch = create_category(
            session, name, category_type, idempotency_key=f"demo:category:{name}"
        )
        category_id = int(batch.summary["category_id"])
        update_category(
            session, category_id, icon=icon, idempotency_key=f"demo:category-icon:{name}"
        )
        ids[name] = category_id
    return ids


def _keywords(session: Session, categories: dict[str, int]) -> None:
    keywords = [(pattern, category, False) for pattern, category in PAYEE_KEYWORDS]
    keywords += [(pattern, category, True) for pattern, category in REGEX_KEYWORDS]
    for pattern, category, is_regex in keywords:
        add_keyword(
            session,
            PAYEE_LIST,
            pattern,
            categories[category],
            is_regex=is_regex,
            idempotency_key=f"demo:keyword:payee:{pattern}",
        )


def _rules(session: Session, categories: dict[str, int], contacts: dict[str, int]) -> None:
    transfer = categories["Transfer"]
    by_transfer = (PaymentMethod.transfer,)
    drafts = (
        (
            "demo:rule:rent-share",
            RuleDraft(
                to_category_id=categories["Rent"],
                paid_by=by_transfer,
                counterparty="contact",
                contact_id=contacts["Sam"],
            ),
        ),
        (
            "demo:rule:loan",
            RuleDraft(
                to_category_id=categories["Loans"],
                paid_by=by_transfer,
                at_least_cents=10_000,
                counterparty="person",
            ),
        ),
        (
            "demo:rule:small-person",
            RuleDraft(
                to_category_id=categories["Dining"],
                paid_by=by_transfer,
                below_cents=4_000,
                counterparty="person",
            ),
        ),
        (
            "demo:rule:transfer",
            RuleDraft(
                to_category_id=transfer,
                paid_by=(PaymentMethod.transfer, PaymentMethod.check, PaymentMethod.cash),
            ),
        ),
    )
    for key, draft in drafts:
        add_rule(session, draft, idempotency_key=key)


def _check_import(report: ImportReport) -> None:
    if report.failures:
        errors = "; ".join(result.error for result in report.files if result.error)
        raise RuntimeError(f"The demo statements did not all import: {errors}")
    failed = [check for check in report.checks if not check.status.ok]
    if failed:
        raise RuntimeError(f"{len(failed)} demo account(s) failed the balance check")


def _recurring(session: Session, today: date) -> None:
    """Decide every suggestion but the held ones, then name the confirmed series. Deciding one can
    let detection find another in the same charges, so it runs until a pass decides nothing."""
    decided = True
    while decided:
        decided = False
        for suggestion in detect(session, today=today):
            if suggestion.name.startswith(HELD_FOR_REVIEW):
                continue
            key = f"demo:recurring:{suggestion.payee_pattern}:{suggestion.amount_cents}"
            if suggestion.name.startswith(tuple(CONFIRMED)):
                confirm(session, suggestion, idempotency_key=key)
            else:
                dismiss(session, suggestion, idempotency_key=key)
            decided = True
    confirmed = session.scalars(
        select(RecurringSeries).where(RecurringSeries.status == SeriesStatus.confirmed)
    ).all()
    for series in confirmed:
        name = next(
            (label for prefix, label in CONFIRMED.items() if series.name.startswith(prefix)), None
        )
        if name is not None:
            update_series(session, series.id, name=name, idempotency_key=f"demo:series:{name}")


def _budgets(session: Session, categories: dict[str, int], today: date) -> None:
    current = date(today.year, today.month, 1)
    months = [_add_months(current, -back) for back in (2, 1, 0)]
    for month in months:
        for name, cents in BUDGETS:
            set_line(
                session,
                month,
                categories[name],
                BudgetKind.category,
                cents,
                idempotency_key=f"demo:budget:{month:%Y-%m}:{name}",
            )


def _goals(session: Session, accounts: dict[str, Account], today: date) -> None:
    create_goal(
        session,
        name="Emergency fund",
        target_cents=1_500_000,
        linked_account_id=accounts["emergency"].id,
        monthly_contribution_cents=60_000,
        icon="shield",
        idempotency_key="demo:goal:emergency",
    )
    create_goal(
        session,
        name="Japan trip",
        target_cents=600_000,
        target_date=_add_months(date(today.year, today.month, 1), 8),
        linked_account_id=accounts["travel"].id,
        monthly_contribution_cents=30_000,
        icon="plane",
        idempotency_key="demo:goal:japan",
    )


def _add_months(day: date, months: int) -> date:
    total = day.year * 12 + day.month - 1 + months
    return date(total // 12, total % 12 + 1, 1)
