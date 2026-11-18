"""The vault stand-in for demo mode: no vault session, so no bank logins are read.

Nothing unlocks, and nothing reads the keychain or runs a vault CLI, so a demo never reaches a
bank or a password manager.
"""

from omsync.core.vault import LoginCredentials, LoginSummary, Vault, VaultError, VaultLocked

MESSAGE = "Bank sync and vault logins are off in the demo."


class DemoVault(Vault):
    """Refuses every unlock and reads nothing. Its state is always locked."""

    def __repr__(self) -> str:
        return "DemoVault()"

    @property
    def idle_minutes(self) -> int:
        return 0

    def unlock(self, password: str) -> None:
        raise VaultError(MESSAGE)

    def is_unlocked(self) -> bool:
        return False

    def lock(self) -> None:
        return None

    def find_logins(self, search: str) -> list[LoginSummary]:
        raise VaultLocked(MESSAGE)

    def get_login(self, item_id: str) -> LoginCredentials:
        raise VaultLocked(MESSAGE)
