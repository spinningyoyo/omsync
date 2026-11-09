"""The vault stand-in for demo mode: no Bitwarden session, so no bank logins are read.

It has the methods the app calls on a Vault. Nothing unlocks, and nothing reads the keychain or
runs `bw`, so a demo never reaches a bank or a password manager.
"""

from omsync.core.vault import LoginCredentials, LoginSummary, VaultError, VaultLocked

MESSAGE = "Bank sync and vault logins are off in the demo."


class DemoVault:
    """Refuses every unlock and reads nothing. Its state is always locked."""

    def __repr__(self) -> str:
        return "DemoVault()"

    idle_minutes = 0

    def unlock(self, email: str, password: str) -> None:
        raise VaultError(MESSAGE)

    def is_unlocked(self) -> bool:
        return False

    def lock(self) -> None:
        return None

    def find_logins(self, search: str) -> list[LoginSummary]:
        raise VaultLocked(MESSAGE)

    def get_login(self, item_id: str) -> LoginCredentials:
        raise VaultLocked(MESSAGE)
