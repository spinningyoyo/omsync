"""Record a real bank download, to reconstruct its request.

    uv run python scripts/bank_record.py https://secure.chase.com/web/auth/

Opens Chrome on a temporary profile. Sign in and download a statement in that window, then press
Enter here, or touch <capture>/control/stop from another process. Touching control/snap saves
cookies and browser storage without stopping. Stopping saves them too. Then read the capture with
scripts/bank_trace.py and delete it: it holds live session cookies and statement files.

The capture goes in a new temporary folder unless a path is given as the second argument. Sign-in
request bodies and fields named like a password are not written. See
docs/architecture/bank-requests/README.md.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import websocket

from omsync.core.settings import load_settings

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9333
MAX_BODY = 8_000_000
TEXT_TYPES = {"Document", "XHR", "Fetch", "Script", "Other"}
SENSITIVE_URL = re.compile(
    r"log.?[io]n|sign.?[io]n|sign.?on|auth|otp|mfa|passw|credential|verif|challenge", re.IGNORECASE
)
# Field names only: a random token can hold these letters by chance.
SENSITIVE_FIELD = re.compile(
    r"""(?:^|[&{,"\s])[\w.\-]*(?:passw|passcode|pwd|otp|ssn|secret)[\w.\-]*"?\s*[=:]""",
    re.IGNORECASE,
)
STORAGE_SCRIPT = (
    "JSON.stringify({href: location.origin + location.pathname, "
    "local: Object.fromEntries(Object.entries(localStorage)), "
    "session: Object.fromEntries(Object.entries(sessionStorage))})"
)


def checked_folder(argument: str | None) -> Path:
    """The capture folder, refused where a capture could be committed or reach real data."""
    if argument is None:
        return Path(tempfile.mkdtemp(prefix="omsync-bank-capture-"))
    folder = Path(argument).resolve()
    for forbidden in (Path(__file__).resolve().parent.parent, load_settings().data_dir.resolve()):
        if folder.is_relative_to(forbidden):
            sys.exit(f"a capture cannot go inside {forbidden}")
    return folder


def safe_body(url: str, body: str | None) -> str | dict[str, Any] | None:
    if body is None:
        return None
    if SENSITIVE_URL.search(url) or SENSITIVE_FIELD.search(body):
        return {"redacted": True, "length": len(body)}
    return body


class Recorder:
    def __init__(self, folder: Path, browser_url: str) -> None:
        self.folder = folder
        self.socket = websocket.create_connection(browser_url, suppress_origin=True, timeout=1)
        self.log = (folder / "events.jsonl").open("a", buffering=1)
        self.next_id = 0
        self.waiting: dict[int, Any] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.requests: dict[str, tuple[str, str | None]] = {}
        self.body_count = 0

    def send(self, method: str, params: dict | None = None, session=None, on_reply=None) -> None:
        self.next_id += 1
        message: dict[str, Any] = {"id": self.next_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        if on_reply:
            self.waiting[self.next_id] = on_reply
        self.socket.send(json.dumps(message))

    def write(self, kind: str, **fields: Any) -> None:
        self.log.write(json.dumps({"t": time.time(), "kind": kind, **fields}) + "\n")

    def attach_all(self) -> None:
        auto_attach = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
        self.send(
            "Browser.setDownloadBehavior",
            {
                "behavior": "allow",
                "downloadPath": str(self.folder / "downloads"),
                "eventsEnabled": True,
            },
        )
        self.send("Target.setDiscoverTargets", {"discover": True})
        self.send("Target.setAutoAttach", auto_attach)

    def on_attached(self, params: dict[str, Any]) -> None:
        session, target = params["sessionId"], params["targetInfo"]
        self.sessions[session] = target
        self.write("target", type=target.get("type"), url=target.get("url", "").split("?")[0])
        self.send(
            "Network.enable",
            {
                "maxPostDataSize": 262144,
                "maxTotalBufferSize": 200_000_000,
                "maxResourceBufferSize": 50_000_000,
            },
            session,
        )
        self.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
            session,
        )
        self.send("Runtime.runIfWaitingForDebugger", {}, session)

    def save_body(self, request_id: str, session: str | None) -> None:
        def saved(reply: dict[str, Any]) -> None:
            result = reply.get("result")
            if not result or result.get("base64Encoded") or len(result["body"]) > MAX_BODY:
                return
            self.body_count += 1
            name = f"{self.body_count}.txt"
            (self.folder / "bodies" / name).write_text(result["body"])
            self.write("body", rid=request_id, file=name, length=len(result["body"]))

        self.send("Network.getResponseBody", {"requestId": request_id}, session, saved)

    def on_event(self, message: dict[str, Any]) -> None:
        method, params = message["method"], message.get("params", {})
        session = message.get("sessionId")
        if method == "Target.attachedToTarget":
            self.on_attached(params)
        elif method == "Network.requestWillBeSent":
            request = params["request"]
            self.requests[params["requestId"]] = (request["url"], params.get("type"))
            self.write(
                "request",
                rid=params["requestId"],
                url=request["url"],
                method=request["method"],
                type=params.get("type"),
                headers=request.get("headers"),
                post=safe_body(request["url"], request.get("postData")),
                has_post=request.get("hasPostData", False),
                initiator=params.get("initiator"),
            )
        elif method == "Network.requestWillBeSentExtraInfo":
            self.write("request_extra", rid=params["requestId"], headers=params.get("headers", {}))
        elif method == "Network.responseReceived":
            response = params["response"]
            self.write(
                "response",
                rid=params["requestId"],
                url=response["url"],
                status=response["status"],
                mime=response.get("mimeType"),
                type=params.get("type"),
                headers=response.get("headers"),
            )
        elif method == "Network.responseReceivedExtraInfo":
            self.write("response_extra", rid=params["requestId"], headers=params.get("headers"))
        elif method == "Network.loadingFinished":
            url, kind = self.requests.get(params["requestId"], ("", None))
            if kind in TEXT_TYPES and not url.startswith("data:"):
                self.save_body(params["requestId"], session)
        elif method == "Browser.downloadWillBegin":
            self.write("download", url=params.get("url", ""), name=params.get("suggestedFilename"))

    def snapshot(self) -> None:
        stamp = int(time.time())
        snaps = self.folder / "snaps"

        def cookies(reply: dict[str, Any]) -> None:
            found = reply.get("result", {}).get("cookies", [])
            (snaps / f"cookies-{stamp}.json").write_text(json.dumps(found))

        self.send("Storage.getCookies", on_reply=cookies)
        for session, target in list(self.sessions.items()):
            if target.get("type") != "page":
                continue

            def storage(reply: dict[str, Any], name: str = session[:6]) -> None:
                value = reply.get("result", {}).get("result", {}).get("value")
                if value:
                    (snaps / f"storage-{stamp}-{name}.json").write_text(value)

            expression = {"expression": STORAGE_SCRIPT, "returnByValue": True}
            self.send("Runtime.evaluate", expression, session, storage)
        self.write("snapshot", stamp=stamp)

    def pump(self) -> bool:
        """Handle one message, if one came. False when Chrome has gone."""
        try:
            raw = self.socket.recv()
        except websocket.WebSocketTimeoutException:
            return True
        except (OSError, websocket.WebSocketException):
            return False
        message = json.loads(raw)
        if "id" in message:
            on_reply = self.waiting.pop(message["id"], None)
            if on_reply:
                on_reply(message)
        elif "method" in message:
            self.on_event(message)
        return True


def main() -> None:
    if len(sys.argv) not in (2, 3) or not sys.argv[1].startswith(("http://", "https://")):
        sys.exit(__doc__)
    os.umask(0o077)
    folder = checked_folder(sys.argv[2] if len(sys.argv) == 3 else None)
    for name in ("profile", "bodies", "downloads", "control", "snaps"):
        (folder / name).mkdir(parents=True, exist_ok=True)
    chrome = subprocess.Popen(
        [
            CHROME,
            f"--remote-debugging-port={PORT}",
            f"--user-data-dir={folder / 'profile'}",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    browser_url = None
    for _ in range(100):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1) as page:
                browser_url = json.load(page)["webSocketDebuggerUrl"]
            break
        except OSError:
            time.sleep(0.2)
    if browser_url is None:
        chrome.terminate()
        sys.exit("Chrome did not open its debugging port")

    recorder = Recorder(folder, browser_url)
    recorder.attach_all()
    recorder.send("Target.createTarget", {"url": sys.argv[1]})
    print(f"Recording to {folder}. Press Enter when finished.", flush=True)

    control = folder / "control"
    threading.Thread(
        target=lambda: sys.stdin.readline() and (control / "stop").touch(), daemon=True
    ).start()
    while chrome.poll() is None and recorder.pump():
        if (control / "snap").exists():
            (control / "snap").unlink()
            recorder.snapshot()
        if (control / "stop").exists():
            recorder.snapshot()
            deadline = time.time() + 2
            while time.time() < deadline and recorder.pump():
                pass
            break
    chrome.terminate()
    chrome.wait(timeout=10)
    print(f"Stopped. Trace it, then delete {folder}", flush=True)


if __name__ == "__main__":
    main()
