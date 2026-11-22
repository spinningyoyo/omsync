"""Vault logins for the Settings pages: unlocking behind the sign-in rate limit, searching for a
login, how a card shows a provider's login, naming that login in its edit dialog while the vault
stays unlocked, and the vault's status line.

A failure is VaultActionError, whose message is safe to show.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from omsync.core.sessions import LoginRateLimiter
from omsync.core.vault import LoginSummary, Vault, VaultError, VaultLocked

SEARCH_MAX = 100
SEARCH_LIMIT = 20
TOO_MANY_ATTEMPTS = "Too many failed attempts. Wait a minute, then try again."
NO_VAULT = "No vault is connected. Connect one with the vault button on the Bank sync page."
MANUAL_SIGN_IN = "You sign in at the bank"
LOGIN_PICKED = "Picked"
LOGIN_NOT_PICKED = "Not picked"


@dataclass(frozen=True)
class VaultStatusView:
    """Whether the vault is unlocked, for the status line on Bank sync and in the user menu."""

    unlocked: bool
    label: str
    detail: str  # when it locks, or what unlocks it


def vault_status_view(vault: Vault | None) -> VaultStatusView | None:
    """A vault past its idle timeout reads as locked. No vault has no status to show."""
    if vault is None:
        return None
    if not vault.is_unlocked():
        return VaultStatusView(False, "Vault locked", "Unlock it here, or when a sync needs it.")
    minutes = vault.idle_minutes
    if minutes <= 0:
        detail = "Stays unlocked until you sign out."
    else:
        unit = "minute" if minutes == 1 else "minutes"
        detail = f"Locks after {minutes} {unit} unused."
    return VaultStatusView(True, "Vault unlocked", detail)


class VaultActionError(Exception):
    """A vault action that cannot be done. The message is safe to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def unlock_vault(vault: Vault | None, limiter: LoginRateLimiter, password: str) -> None:
    """Unlock the vault. A failure counts toward the sign-in limit, so this form cannot be used to
    guess the master password fast."""
    if vault is None:
        raise VaultActionError(NO_VAULT)
    if limiter.is_blocked():
        raise VaultActionError(TOO_MANY_ATTEMPTS)  # 422: htmx swaps 422 bodies, not 429
    try:
        vault.unlock(password)
    except VaultError as exc:
        limiter.record_failure()
        raise VaultActionError(str(exc)) from None


class LoginNames:
    """Vault login names by item ID, kept while the vault stays unlocked. Every lookup runs the
    vault CLI, which takes seconds, so an edit dialog opened again, or a login just found by a
    search, needs no new lookup. A name is only a label: it
    can be out of date after a rename in the vault until the vault locks. A login not found is
    remembered too, until the vault locks or the person searches the vault again."""

    def __init__(self) -> None:
        self._names: dict[str, str] = {}
        self._missing: set[str] = set()

    def get(self, item_id: str) -> str | None:
        return self._names.get(item_id)

    def is_missing(self, item_id: str) -> bool:
        return item_id in self._missing

    def remember(self, logins: Iterable[LoginSummary]) -> None:
        for login in logins:
            self._names[login.id] = login.name or "Unnamed login"

    def remember_missing(self, item_id: str) -> None:
        self._missing.add(item_id)

    def forget_missing(self) -> None:
        self._missing.clear()

    def clear(self) -> None:
        self._names.clear()
        self._missing.clear()


def login_label(vault: Vault | None, vault_item_id: str | None) -> str:
    """How a card shows a provider's login. It never asks the vault, so it reads the same whether
    the vault is locked or not."""
    if vault is None:
        return MANUAL_SIGN_IN
    return LOGIN_PICKED if vault_item_id else LOGIN_NOT_PICKED


def vault_login_name(
    vault: Vault | None,
    vault_item_id: str | None,
    names: LoginNames,
    query_hints: tuple[str, ...] = (),
) -> str:
    """The name of a provider's vault login, for its edit dialog. A locked vault, or no login,
    gives the card's label. The vault has no lookup by ID that does not also fetch the password, so
    a name is found by searching with the hints, and every login a search returns is remembered in
    names."""
    if vault is None or vault_item_id is None or not vault.is_unlocked():
        return login_label(vault, vault_item_id)
    known = names.get(vault_item_id)
    if known is not None:
        return known
    if names.is_missing(vault_item_id):
        return "Not found in the vault"
    try:
        for query in filter(None, query_hints):
            names.remember(vault.find_logins(query))
            known = names.get(vault_item_id)
            if known is not None:
                return known
    except VaultLocked:
        names.clear()
        return login_label(vault, vault_item_id)
    except VaultError:
        return "Vault lookup failed"
    names.remember_missing(vault_item_id)
    return "Not found in the vault"


def search_logins(vault: Vault, query: str, names: LoginNames | None = None) -> list[LoginSummary]:
    """Login items whose name matches the query, at most SEARCH_LIMIT of them. With names, the
    logins found are remembered, so showing one later needs no search."""
    try:
        found = vault.find_logins(query[:SEARCH_MAX])
    except VaultError as exc:
        raise VaultActionError(str(exc)) from None
    if names is not None:
        names.remember(found)
        names.forget_missing()
    return found[:SEARCH_LIMIT]
