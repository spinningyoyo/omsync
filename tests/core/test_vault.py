"""Vault wrapper tests against a fake `bw` executable.

Nothing here touches the real keychain, a real `bw`, or the real home directory.
"""

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from omsync.core.vault import (
    CLIENT_ID_ENTRY,
    CLIENT_SECRET_ENTRY,
    BitwardenCLI,
    Vault,
)

EMAIL = "ana@example.test"
PASSWORD = "correct-horse-battery"
CLIENT_ID = "user.client-id-value"
CLIENT_SECRET = "client-secret-value"
SECRET_ENV_NAMES = ("BW_SESSION", "BW_CLIENTID", "BW_CLIENTSECRET", "BW_PASSWORD")
ITEMS = [
    {
        "id": "item-alpha",
        "type": 1,
        "name": "Bank Alpha",
        "login": {"username": "alpha-user", "password": "alpha-pass", "totp": "totp-seed-alpha"},
    },
    {
        "id": "item-beta",
        "type": 1,
        "name": "Bank Beta",
        "login": {"username": "beta-user", "password": "beta-pass", "totp": None},
    },
    {"id": "item-note", "type": 2, "name": "Bank note", "secureNote": {"type": 0}},
]

# The body of the fake `bw`. It keeps state in FAKE_BW_STATE and appends one JSON line per
# call to FAKE_BW_LOG with argv and the sorted env key names (never values).
FAKE_BW_BODY = """\
import json
import os
import sys
import uuid

STATE_PATH = os.environ["FAKE_BW_STATE"]
LOG_PATH = os.environ["FAKE_BW_LOG"]
args = sys.argv[1:]
env = os.environ

with open(LOG_PATH, "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": args, "env": sorted(env)}) + "\\n")

with open(STATE_PATH, encoding="utf-8") as handle:
    state = json.load(handle)


def save():
    with open(STATE_PATH, "w", encoding="utf-8") as handle:
        json.dump(state, handle)


def fail(message):
    sys.stderr.write(message + "\\n")
    sys.exit(1)


def require_session():
    key = state.get("session")
    if not key or env.get("BW_SESSION") != key:
        fail("Vault is locked.")


command = args[0] if args else ""
if command == "status":
    print(json.dumps({"status": state["status"], "userEmail": state["email"]}))
elif args == ["login", "--apikey"]:
    if env.get("BW_CLIENTID") != state["client_id"]:
        fail("Invalid client id.")
    if env.get("BW_CLIENTSECRET") != state["client_secret"]:
        fail("Invalid client secret.")
    state["status"] = "locked"
    save()
elif command == "unlock":
    if state["status"] == "unauthenticated":
        fail("You are not logged in.")
    attempt = env.get(args[args.index("--passwordenv") + 1], "")
    if attempt != state["password"]:
        fail("wrong password " + attempt)
    state["session"] = uuid.uuid4().hex
    state["status"] = "unlocked"
    save()
    print(state["session"])
elif command == "lock":
    state["session"] = None
    state["status"] = "locked"
    save()
elif args[:2] == ["list", "items"]:
    require_session()
    needle = args[args.index("--search") + 1].lower()
    print(json.dumps([i for i in state["items"] if needle in i["name"].lower()]))
elif args[:2] == ["get", "item"]:
    require_session()
    matches = [i for i in state["items"] if i["id"] == args[2]]
    if not matches:
        fail("Not found.")
    print(json.dumps(matches[0]))
else:
    fail("Unknown command " + command)
"""


class FakeKeychain:
    def __init__(self, entries: Mapping[str, str]) -> None:
        self._entries = dict(entries)

    def get(self, name: str) -> str | None:
        return self._entries.get(name)


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _initial_state() -> dict[str, object]:
    return {
        "status": "locked",
        "email": EMAIL,
        "password": PASSWORD,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "session": None,
        "items": ITEMS,
    }


class FakeBW:
    """A fake `bw` executable with its state file and calls log under tmp_path."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.executable = root / "bw"
        self.state_path = root / "bw-state.json"
        self.log_path = root / "bw-calls.jsonl"
        self.appdata_dir = root / "bw-appdata"
        self.executable.write_text(f"#!{sys.executable}\n{FAKE_BW_BODY}", encoding="utf-8")
        self.executable.chmod(0o755)
        monkeypatch.setenv("FAKE_BW_STATE", str(self.state_path))
        monkeypatch.setenv("FAKE_BW_LOG", str(self.log_path))
        for name in SECRET_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        self.set_state(status="unauthenticated")
        self.cli = BitwardenCLI(str(self.executable), appdata_dir=self.appdata_dir)
        self.keychain = FakeKeychain(
            {CLIENT_ID_ENTRY: CLIENT_ID, CLIENT_SECRET_ENTRY: CLIENT_SECRET}
        )
        self.clock = FakeClock()

    def set_state(self, **changes: object) -> None:
        state = self.read_state() if self.state_path.exists() else _initial_state()
        state.update(changes)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def read_state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def calls(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def argvs(self) -> list[list[str]]:
        return [call["argv"] for call in self.calls()]

    def vault(self, idle_minutes: int = 15) -> Vault:
        return Vault(self.cli, self.keychain, idle_minutes, clock=self.clock)


@pytest.fixture
def bw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBW:
    return FakeBW(tmp_path, monkeypatch)


def test_password_never_appears_in_argv_or_the_calls_log(bw: FakeBW) -> None:
    vault = bw.vault()

    vault.unlock(EMAIL, PASSWORD)
    vault.find_logins("bank")

    assert ["unlock", "--passwordenv", "OMSYNC_BW_PASSWORD", "--raw"] in bw.argvs()
    for argv in bw.argvs():
        assert PASSWORD not in " ".join(argv)
    assert PASSWORD not in bw.log_path.read_text(encoding="utf-8")


def test_os_environ_is_unchanged_by_unlock_and_get_login(bw: FakeBW) -> None:
    bw.set_state(status="locked")
    before = dict(os.environ)
    vault = bw.vault()

    vault.unlock(EMAIL, PASSWORD)
    vault.get_login("item-alpha")

    assert dict(os.environ) == before
    assert "BW_SESSION" not in os.environ
