"""Helpers shared by the web tests: a fake vault, a fake clock and the sign-in call.

Nothing here runs `bw`, reads the keychain, or touches the real data directory.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi.testclient import TestClient

from omsync.core.vault import VaultError

EMAIL = "ana@example.test"
PASSWORD = "correct-horse-battery"
SESSION_COOKIE = "omsync_session"
MakeClient = Callable[..., TestClient]


@dataclass
class FakeVault:
    """Stands in for the Bitwarden vault. It accepts one email and password and records calls."""

    unlocked: bool = False
    unlock_calls: list[str] = field(default_factory=list)  # emails only, never passwords
    lock_calls: int = 0

    def unlock(self, email: str, password: str) -> None:
        self.unlock_calls.append(email)
        if email != EMAIL:
            raise VaultError("Email does not match this vault")
        if password != PASSWORD:
            raise VaultError("Incorrect master password")
        self.unlocked = True

    def lock(self) -> None:
        self.lock_calls += 1
        self.unlocked = False

    def is_unlocked(self) -> bool:
        return self.unlocked


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def sign_in(client: TestClient, next_path: str = "/") -> None:
    response = client.post(
        "/sign-in",
        data={"email": EMAIL, "password": PASSWORD, "next": next_path},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
