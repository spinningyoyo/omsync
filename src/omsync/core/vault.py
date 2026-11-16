"""The vault: where a sync reads bank logins. Each vault implements Vault; bitwarden.py is one.

A vault's secrets stay in memory. They never appear in argv, in os.environ, in an exception
message, or in a repr.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Protocol

import keyring

KEYCHAIN_SERVICE = "omsync"


class VaultError(Exception):
    """The vault could not do what was asked. The message is safe to show."""


class VaultLocked(VaultError):
    """The vault has no usable session. Unlock it first."""


class Keychain(Protocol):
    def get(self, name: str) -> str | None: ...


class SystemKeychain:
    """Reads omsync's entries from the system keychain."""

    def get(self, name: str) -> str | None:
        return keyring.get_password(KEYCHAIN_SERVICE, name)


@dataclass(frozen=True)
class LoginSummary:
    id: str
    name: str
    username: str | None


@dataclass(frozen=True, repr=False)
class LoginCredentials:
    """A bank login from the vault. uris are the item's URIs in the vault's order; the first one
    is where the login page is. They are left out of repr, which shows only the masked fields."""

    id: str
    name: str
    username: str | None
    password: str
    totp: str | None
    uris: tuple[str, ...] = ()

    def __repr__(self) -> str:
        masked_totp = "'***'" if self.totp else "None"
        return (
            f"LoginCredentials(id={self.id!r}, name={self.name!r}, "
            f"username={self.username!r}, password='***', totp={masked_totp})"
        )


class Vault(ABC):
    """A vault session that locks after an idle timeout. idle_minutes=0 means it never does."""

    @property
    @abstractmethod
    def idle_minutes(self) -> int: ...

    @abstractmethod
    def unlock(self, password: str) -> None:
        """Open a session with the master password, or raise VaultError."""

    @abstractmethod
    def is_unlocked(self) -> bool:
        """Whether a session is open. Reading it locks a vault whose idle timeout has passed."""

    @abstractmethod
    def lock(self) -> None: ...

    def disconnect(self) -> None:
        """Lock, and forget the account, when omsync stops using this vault."""
        self.lock()

    @abstractmethod
    def find_logins(self, search: str) -> list[LoginSummary]:
        """Login items whose name matches search. Raises VaultLocked when locked."""

    @abstractmethod
    def get_login(self, item_id: str) -> LoginCredentials:
        """One login with its password. Raises VaultLocked when locked."""


def is_locked(vault: Vault | None) -> bool:
    """True when there is a vault and it must be unlocked first. No vault is never locked."""
    return vault is not None and not vault.is_unlocked()
