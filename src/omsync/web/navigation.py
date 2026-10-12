"""The icon rail: which pages exist, how they are grouped, and their icons.

Groups are separated by dividers. Settings and sign-out live in the user menu, not here.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    href: str
    icon: str


NAV_GROUPS: tuple[tuple[NavItem, ...], ...] = (
    (
        NavItem("home", "Home", "/", "house"),
        NavItem("reports", "Reports", "/reports", "chart-pie"),
    ),
    (
        NavItem("transactions", "Transactions", "/transactions", "credit-card"),
        NavItem("recurring", "Recurring", "/recurring", "calendar-sync"),
        NavItem("reconcile", "Reconcile", "/reconcile", "handshake"),
        NavItem("categories", "Categories", "/categories", "shapes"),
    ),
    (NavItem("plan", "Plan", "/plan", "land-plot"),),
)
