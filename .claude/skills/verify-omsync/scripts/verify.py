#!/usr/bin/env python3
"""Launch, check, drive and clean up a throwaway omsync for verification.

Run from the repository root with uv, so omsync and Playwright come from the project:

    uv run python .claude/skills/verify-omsync/scripts/verify.py launch [--mode ledger|demo]
    uv run python .claude/skills/verify-omsync/scripts/verify.py doctor RUN
    uv run python .claude/skills/verify-omsync/scripts/verify.py drive RUN SCRIPT.py [--theme dark]
    uv run python .claude/skills/verify-omsync/scripts/verify.py cleanup RUN

RUN is the directory that launch prints. It holds run.json, the server's data and log, and
evidence/, which cleanup keeps.

ledger mode serves the demo's made-up data as an ordinary ledger, so every page behaves as it does
for the owner. Only the two systems outside omsync are stand-ins: the computer's owner check always
passes, and Bitwarden is an in-memory vault whose master password is MASTER_PASSWORD. Sync is off,
because a sync signs in to real banks and stores. demo mode runs `omsync serve --demo` itself, for
the demo's own sign-in and Reset demo.

Nothing here reads or writes the owner's data directory or config file.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
MASTER_PASSWORD = "verify-master"  # noqa: S105 - the stand-in vault's, not a real secret
VAULT_EMAIL = "verify@example.test"
READY_TIMEOUT = 240
SYNC_OFF = "Sync is off in verification runs: a sync signs in to real banks and stores."


# Launch and cleanup


def launch(mode: str) -> Path:
    run = Path(tempfile.mkdtemp(prefix="omsync-verify-"))
    port = _free_port()
    (run / "evidence").mkdir()
    log = (run / "serve.log").open("w")
    env = {
        **os.environ,
        "OMSYNC_DATA_DIR": str(run / "data"),
        "OMSYNC_CONFIG": str(run / "omsync.toml"),
        "OMSYNC_HOST": "127.0.0.1",
        "OMSYNC_PORT": str(port),
    }
    if mode == "demo":
        command = ["uv", "run", "omsync", "serve", "--demo"]
    else:
        command = ["uv", "run", "python", str(Path(__file__)), "_serve", str(run), str(port)]
    proc = subprocess.Popen(
        command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
    )
    info = {
        "mode": mode,
        "pid": proc.pid,
        "port": port,
        "base": f"http://127.0.0.1:{port}",
        "data_dir": str(run / "data"),
        "commit": _git("rev-parse", "HEAD"),
        "tree": _tree_digest(),
        "started": time.time(),
    }
    (run / "run.json").write_text(json.dumps(info, indent=2))
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise SystemExit(f"the server exited early; see {run / 'serve.log'}")
        if _health(info["base"]):
            print(run)
            return run
        time.sleep(0.5)
    cleanup(run)
    raise SystemExit(f"the server did not answer /health in {READY_TIMEOUT}s; see {run}/serve.log")


def cleanup(run: Path) -> None:
    """Stop the process group this run started and delete its data. evidence/ stays."""
    info = _info(run)
    try:
        os.killpg(info["pid"], signal.SIGTERM)
        for _ in range(40):
            os.killpg(info["pid"], 0)
            time.sleep(0.25)
        os.killpg(info["pid"], signal.SIGKILL)
    except ProcessLookupError:
        pass
    data = Path(info["data_dir"])
    if data.is_dir() and data.parent == run:
        subprocess.run(["rm", "-rf", str(data)], check=False)
    print(f"stopped; evidence kept in {run / 'evidence'}")


# Doctor


def doctor(run: Path) -> bool:
    """Whether this run's server is worth driving. Read-only."""
    info = _info(run)
    checks = [
        ("process alive", _alive(info["pid"])),
        ("/health answers", _health(info["base"])),
        ("port owned by this run", _port_owner(info["port"]) in _group_pids(info["pid"])),
        ("data dir is this run's", Path(info["data_dir"]).parent == run),
        ("code unchanged since launch", info["tree"] == _tree_digest()),
    ]
    for name, ok in checks:
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
    if not checks[-1][1]:
        print("     the server runs the code from launch time; clean up and launch again")
    return all(ok for _, ok in checks)


# Drive


def drive(run: Path, script: Path, theme: str) -> None:
    """Run a Playwright script against this run, signed in. The script gets page, base, shot,
    settle and MASTER_PASSWORD as globals; see SKILL.md."""
    from playwright.sync_api import sync_playwright

    info = _info(run)
    base = info["base"]
    evidence = run / "evidence"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.add_init_script(
            f"localStorage.setItem('omsync-theme', {json.dumps(theme)});"
            "localStorage.setItem('omsync-privacy-mode', 'false');"
        )
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        _sign_in(page, base, info["mode"])

        def settle() -> None:
            page.wait_for_load_state("networkidle")
            page.wait_for_function("() => !document.querySelector('.htmx-request')")

        def shot(name: str) -> None:
            settle()
            page.screenshot(path=str(evidence / f"{name}.png"), full_page=True)
            (evidence / f"{name}.aria.txt").write_text(page.locator("body").aria_snapshot())
            print(f"evidence: {evidence / name}.png")

        code = compile(script.read_text(), str(script), "exec")
        try:
            exec(  # noqa: S102 - the agent's own drive script
                code,
                {
                    "page": page,
                    "base": base,
                    "shot": shot,
                    "settle": settle,
                    "MASTER_PASSWORD": MASTER_PASSWORD,
                    "__name__": "__drive__",
                },
            )
        except Exception:
            shot("failure")
            raise
        finally:
            if errors:
                (evidence / "browser-errors.txt").write_text("\n".join(errors))
                print(f"browser errors: {evidence / 'browser-errors.txt'}")
            browser.close()


def _sign_in(page, base: str, mode: str) -> None:
    page.goto(f"{base}/sign-in")
    name = "Explore the demo" if mode == "demo" else "Sign in"
    page.get_by_role("button", name=name, exact=True).click()
    page.wait_for_url(lambda url: "/sign-in" not in url)


# The ledger-mode server


def _serve(run: Path, port: int) -> None:
    import uvicorn

    from omsync.core.settings import Settings
    from omsync.core.vault import LoginCredentials, LoginSummary, Vault, VaultError, VaultLocked
    from omsync.demo.build import build_demo
    from omsync.web.app import create_app
    from omsync.web.sync_runner import SyncRefused

    logins = [
        LoginSummary("verify-chase", "Chase (verify)", "owner@example.test"),
        LoginSummary("verify-amazon", "Amazon (verify)", "owner@example.test"),
        LoginSummary("verify-costco", "Costco (verify)", "owner@example.test"),
        LoginSummary("verify-boa", "Bank of America (verify)", "owner@example.test"),
    ]

    @dataclass
    class StandInVault(Vault):
        """Bitwarden's stand-in: one master password, four logins, no outside calls."""

        unlocked: bool = False

        @property
        def idle_minutes(self) -> int:
            return 15

        def unlock(self, password: str) -> None:
            if password != MASTER_PASSWORD:
                raise VaultError("Incorrect master password")
            self.unlocked = True

        def is_unlocked(self) -> bool:
            return self.unlocked

        def lock(self) -> None:
            self.unlocked = False

        def find_logins(self, search: str) -> list[LoginSummary]:
            self.verify()
            return [x for x in logins if search.lower() in x.name.lower()]

        def get_login(self, item_id: str) -> LoginCredentials:
            raise VaultLocked(SYNC_OFF)

    class OwnerPasses:
        def confirm(self, reason: str) -> bool:
            return True

    data = run / "data"
    build_demo(data, today=date.today())
    settings = Settings(
        data_dir=data, host="127.0.0.1", port=port, email=VAULT_EMAIL, vault="bitwarden"
    )
    app = create_app(settings, vault=StandInVault(), owner_check=OwnerPasses())
    app.state.vault_opener = lambda _settings: StandInVault()

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise SyncRefused(SYNC_OFF)

    app.state.sync_runner.start = refuse
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


# Helpers


def _info(run: Path) -> dict:
    path = run / "run.json"
    if not path.is_file():
        raise SystemExit(f"{run} is not a verification run (no run.json)")
    return json.loads(path.read_text())


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _health(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def _alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _group_pids(pgid: int) -> set[int]:
    out = subprocess.run(
        ["ps", "-g", str(pgid), "-o", "pid="], check=False, capture_output=True, text=True
    )
    return {int(x) for x in out.stdout.split()}


def _port_owner(port: int) -> int | None:
    out = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        check=False,
        capture_output=True,
        text=True,
    )
    pids = out.stdout.split()
    return int(pids[0]) if pids else None


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], check=False, cwd=ROOT, capture_output=True, text=True)
    return out.stdout.strip()


def _tree_digest() -> str:
    """The commit plus the uncommitted changes under src/, so doctor sees edits after launch."""
    import hashlib

    diff = _git("diff", "HEAD", "--", "src") + _git(
        "ls-files", "--others", "--exclude-standard", "src"
    )
    return _git("rev-parse", "HEAD") + ":" + hashlib.sha256(diff.encode()).hexdigest()[:16]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("launch")
    p.add_argument("--mode", choices=("ledger", "demo"), default="ledger")
    for name in ("doctor", "cleanup"):
        sub.add_parser(name).add_argument("run", type=Path)
    p = sub.add_parser("drive")
    p.add_argument("run", type=Path)
    p.add_argument("script", type=Path)
    p.add_argument("--theme", choices=("light", "dark"), default="light")
    p = sub.add_parser("_serve")
    p.add_argument("run", type=Path)
    p.add_argument("port", type=int)
    args = parser.parse_args()
    if args.command == "launch":
        launch(args.mode)
    elif args.command == "doctor":
        sys.exit(0 if doctor(args.run) else 1)
    elif args.command == "drive":
        drive(args.run, args.script, args.theme)
    elif args.command == "cleanup":
        cleanup(args.run)
    else:
        _serve(args.run, args.port)


if __name__ == "__main__":
    main()
