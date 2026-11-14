"""The built wheel, installed into a fresh venv, migrates its database and serves the web app.

This is the only test that builds a package. It runs `uv build --wheel`, creates a venv, installs
the wheel with its dependencies, then runs `omsync db upgrade`, `omsync accounts list`, and
`omsync serve`. It is marked slow, so the default run skips it: run it with
`uv run pytest -m slow`. It is skipped when uv is not on PATH.

HOME, the data directory, and the config path all point into tmp_path, so nothing reads the real
home directory or ~/omsync-data. uv's package cache is shared with the outer run, so the install
does not download everything again. The cache holds packages only, never personal data.
"""

import os
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

REPO_ROOT = Path(__file__).resolve().parents[2]
UV_TIMEOUT = 600  # seconds for each uv call, which may download dependencies


def _run(command: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command, capture_output=True, text=True, env=env, timeout=UV_TIMEOUT, check=False
    )
    assert result.returncode == 0, (
        f"{command[0]} {command[1:3]} failed:\n{result.stdout}{result.stderr}"
    )
    return result


def _isolated_env(home: Path, uv_cache: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "UV_CACHE_DIR": uv_cache,
        "OMSYNC_DATA_DIR": str(home / "data"),
        "OMSYNC_CONFIG": str(home / "absent.toml"),
    }


def _nursery_requirement() -> str:
    """omsync-nursery at the exact commit uv.lock records. A [tool.uv.sources] entry does not
    travel in a wheel's metadata, so the install names the package's source directly, as any
    install of the wheel must."""
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text())
    package = next(p for p in lock["package"] if p["name"] == "omsync-nursery")
    url, commit = package["source"]["git"].split("#")
    return f"omsync-nursery @ git+{url.split('?')[0]}@{commit}"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _get(url: str) -> int | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status
    except (urllib.error.URLError, OSError):
        return None


def test_wheel_installs_migrates_and_serves(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH")
    uv_cache = _run([uv, "cache", "dir"]).stdout.strip()

    dist = tmp_path / "dist"
    _run([uv, "build", "--wheel", "--out-dir", str(dist), str(REPO_ROOT)])
    (wheel,) = dist.glob("omsync-*.whl")
    names = set(zipfile.ZipFile(wheel).namelist())
    # Everything the app loads from disk must travel inside the wheel.
    assert "omsync/migrations/env.py" in names
    assert "omsync/migrations/script.py.mako" in names
    assert any(n.startswith("omsync/migrations/versions/") and n.endswith(".py") for n in names)
    assert "omsync/web/templates/base.html" in names
    assert "omsync/web/static/css/app.css" in names

    venv = tmp_path / "venv"
    _run([uv, "venv", "--python", sys.executable, str(venv)])
    python = str(venv / "bin" / "python")
    _run([uv, "pip", "install", "--python", python, str(wheel), _nursery_requirement()])

    home = tmp_path / "home"
    home.mkdir()
    env = _isolated_env(home, uv_cache)
    omsync = str(venv / "bin" / "omsync")

    upgrade = _run([omsync, "db", "upgrade"], env)
    assert (home / "data" / "omsync.db").is_file(), upgrade.stdout
    assert "is at revision" in upgrade.stdout
    _run([omsync, "accounts", "list"], env)  # refuses unless the database is at head

    port = _free_port()
    server = subprocess.Popen(
        [omsync, "serve"],
        env={**env, "OMSYNC_PORT": str(port)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while _get(f"{base}/health") != 200:
            assert server.poll() is None, "omsync serve exited before it answered"
            assert time.monotonic() < deadline, "omsync serve did not answer /health"
            time.sleep(0.5)
        assert _get(f"{base}/sign-in") == 200  # renders a template from the package
        assert _get(f"{base}/static/css/app.css") == 200  # serves a static file from the package
    finally:
        server.terminate()
        server.wait(timeout=30)
