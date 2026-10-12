"""Vault logins for the Settings pages: unlocking behind the sign-in rate limit, searching for a
login, and naming a provider's login while the vault stays unlocked.

A failure is VaultActionError, whose message is safe to show.
"""

from collections.abc import Iterable

from omsync.core.sessions import LoginRateLimiter
from omsync.core.vault import LoginSummary, Vault, VaultError
from omsync.ingest.institutions import InstitutionGroup, ProviderLine

SEARCH_MAX = 100
SEARCH_LIMIT = 20
TOO_MANY_ATTEMPTS = "Too many failed attempts. Wait a minute, then try again."


class VaultActionError(Exception):
    """A vault action that cannot be done. The message is safe to show."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def unlock_vault(vault: Vault, limiter: LoginRateLimiter, email: str, password: str) -> None:
    """Unlock the vault for the signed-in email. A failure counts toward the sign-in limit, so this
    form cannot be used to guess the master password faster than sign-in allows."""
    if limiter.is_blocked():
        raise VaultActionError(TOO_MANY_ATTEMPTS)  # 422: htmx swaps 422 bodies, not 429
    try:
        vault.unlock(email, password)
    except VaultError as exc:
        limiter.record_failure()
        raise VaultActionError(str(exc)) from None


class LoginNames:
    """Vault login names by item ID, kept while the vault stays unlocked. Every lookup runs the
    Bitwarden CLI, which takes seconds, and the Bank sync list is drawn after each change, so
    without this each save would wait on searches for every provider. A name is only a label: it
    can be out of date after a rename in Bitwarden until the vault locks. A login not found is
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


def vault_login_name(
    vault: Vault,
    provider: ProviderLine,
    institution: InstitutionGroup,
    names: LoginNames,
) -> str:
    """The name of the provider's vault login, or why there is none to show. The vault has no
    lookup by ID that does not also fetch the password, so a name is found by searching, and every
    login a search returns is remembered in names."""
    if provider.vault_item_id is None:
        return "No vault login"
    if not vault.is_unlocked():
        names.clear()
        return "vault locked"
    known = names.get(provider.vault_item_id)
    if known is not None:
        return known
    if names.is_missing(provider.vault_item_id):
        return "Not found in the vault"
    for query in dict.fromkeys((provider.key, institution.name, institution.key)):
        try:
            matches = vault.find_logins(query)
        except VaultError:
            return "Vault lookup failed"
        names.remember(matches)
        known = names.get(provider.vault_item_id)
        if known is not None:
            return known
    names.remember_missing(provider.vault_item_id)
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
