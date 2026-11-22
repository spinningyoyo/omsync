"""Helpers shared by the web tests: a fake vault, a fake owner check, a fake clock and the
sign-in call.

Nothing here runs `bw`, shows a system prompt, reads the keychain, or touches the real data
directory.
"""

from collections.abc import Callable
from dataclasses import dataclass

from fastapi.testclient import TestClient

from omsync.core.vault import LoginSummary, VaultError, VaultLocked

EMAIL = "ana@example.test"
PASSWORD = "correct-horse-battery"
SESSION_COOKIE = "omsync_session"
MakeClient = Callable[..., TestClient]


@dataclass
class FakeVault:
    """Stands in for a vault. It accepts one master password and records calls."""

    unlocked: bool = False
    idle_minutes: int = 15
    unlock_calls: int = 0
    lock_calls: int = 0

    def unlock(self, password: str) -> None:
        self.unlock_calls += 1
        if password != PASSWORD:
            raise VaultError("Incorrect master password")
        self.unlocked = True

    def lock(self) -> None:
        self.lock_calls += 1
        self.unlocked = False

    def disconnect(self) -> None:
        self.lock()

    def is_unlocked(self) -> bool:
        return self.unlocked

    def verify(self) -> None:
        if not self.unlocked:
            raise VaultLocked("Vault is locked; unlock it first")

    def find_logins(self, search: str) -> list[LoginSummary]:
        self.verify()
        return [login for login in FAKE_LOGINS if search.lower() in login.name.lower()]


FAKE_LOGINS = (
    LoginSummary("item-amazon", "Amazon (Ana)", "ana@example.test"),
    LoginSummary("item-chase", "Chase (Ana)", "ana@example.test"),
)


@dataclass
class FakeOwnerCheck:
    """Stands in for the computer's owner check. passes decides the answer; calls counts prompts."""

    passes: bool = True
    calls: int = 0

    def confirm(self, reason: str) -> bool:
        self.calls += 1
        return self.passes


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def sign_in(client: TestClient, next_path: str = "/") -> None:
    response = client.post("/sign-in", data={"next": next_path}, follow_redirects=False)
    assert response.status_code == 303, response.text
