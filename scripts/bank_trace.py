"""Read a capture from scripts/bank_record.py. Prints names, lengths, and places, never values.

    uv run python scripts/bank_trace.py <capture>            # requests that look like downloads
    uv run python scripts/bank_trace.py <capture> <regex>    # trace the requests matching regex

A trace sorts each value a request sent, and names where a long value first appeared: a response
body, a response header, an earlier request, a cookie, or browser storage. See
docs/architecture/bank-requests/README.md.
"""

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlsplit

LIKELY_DOWNLOAD = re.compile(
    r"download|export|statement|transaction|txn|\.csv|\.qfx|\.ofx", re.IGNORECASE
)
ASSET = re.compile(r"\.(js|css|png|gif|svg|woff2?|jpg)(\?|$)", re.IGNORECASE)
TOKEN_NAME = re.compile(r"csrf|xsrf|token|nonce", re.IGNORECASE)
STANDARD_HEADER = re.compile(
    r"accept.*|user-agent|referer|origin|host|connection|content-(length|type)|cache-control"
    r"|pragma|upgrade-insecure-requests|priority|cookie|dnt|sec-.*|:.*",
    re.IGNORECASE,
)
MIN_TRACED_LENGTH = 8  # shorter values match almost everything


class Capture:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        with (folder / "events.jsonl").open() as log:
            self.events = [json.loads(line) for line in log]
        self.start = self.events[0]["t"]
        self.requests = {
            e["rid"]: e
            for e in self.events
            if e["kind"] == "request" and not e["url"].startswith("data:")
        }
        self.sent_headers: dict[str, dict[str, str]] = {}
        for event in self.events:
            if event["kind"] == "request_extra":
                self.sent_headers.setdefault(event["rid"], {}).update(event["headers"])
        self.traffic = list(self._traffic())
        self.stored = list(self._stored())

    def place(self, event: dict[str, Any]) -> str:
        url = urlsplit(self.requests.get(event.get("rid"), {}).get("url", event.get("url", "")))
        return f"{url.netloc}{url.path[:70]}"

    def sent_at(self, event: dict[str, Any]) -> float:
        """When a response's request went out. A body is saved later, possibly after a request
        that used it, so its own time cannot order the traffic."""
        return self.requests.get(event.get("rid"), event)["t"]

    def _traffic(self):
        for event in self.events:
            if event["kind"] == "body":
                body = self.folder / "bodies" / event["file"]
                if body.exists():
                    text = body.read_text(errors="replace")
                    yield self.sent_at(event), f"response body {self.place(event)}", text
            elif event["kind"] == "response_extra":
                for name, value in (event.get("headers") or {}).items():
                    if name.lower() != "content-security-policy":
                        label = f"response header {name} {self.place(event)}"
                        yield self.sent_at(event), label, str(value)
            elif event["kind"] == "request":
                yield event["t"], f"earlier request URL {self.place(event)}", event["url"]
                if isinstance(event.get("post"), str):
                    yield event["t"], f"earlier request body {self.place(event)}", event["post"]

    def _stored(self):
        for path in sorted((self.folder / "snaps").glob("cookies-*")):
            for cookie in json.loads(path.read_text()):
                label = f"cookie {cookie['name']} @{cookie['domain']}"
                yield label, str(cookie.get("value", ""))
        for path in sorted((self.folder / "snaps").glob("storage-*")):
            page = json.loads(path.read_text())
            for area in ("local", "session"):
                for key, value in page[area].items():
                    yield f"{area}Storage {key} @{page['href'][:50]}", str(value)

    def sources(self, value: str, before: float) -> tuple[list[tuple[int, str]], list[str]]:
        forms = {value, unquote(value), quote(value, safe="")}
        earlier = [
            (round(when - self.start), label)
            for when, label, text in self.traffic
            if when < before and any(form in text for form in forms)
        ]
        stored = [label for label, text in self.stored if any(form in text for form in forms)]
        return earlier, stored


def fields_of(capture: Capture, request: dict[str, Any]) -> list[tuple[str, str, str]]:
    found = [
        ("query", key, value)
        for key, value in parse_qsl(urlsplit(request["url"]).query, keep_blank_values=True)
    ]
    body = request.get("post")
    if isinstance(body, str):
        try:
            found += [("json", key, str(value)) for key, value in json.loads(body).items()]
        except (ValueError, AttributeError):
            found += [("form", k, v) for k, v in parse_qsl(body, keep_blank_values=True)]
    headers = {**(request.get("headers") or {}), **capture.sent_headers.get(request["rid"], {})}
    seen = set()
    for name, value in headers.items():
        if not STANDARD_HEADER.fullmatch(name) and name.lower() not in seen:
            seen.add(name.lower())
            found.append(("header", name, value))
    return found


def shape(value: str) -> str:
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}|\d{8}", value):
        return "date"
    if re.fullmatch(r"\d+", value):
        return "digits"
    if re.fullmatch(r"[A-Za-z_ -]+", value) and len(value) <= 24:
        return f"word: {value}"
    return "opaque"


def _print_origins(capture: Capture, request: dict, value: str) -> None:
    earlier, stored = capture.sources(value, request["t"])
    for when, label in earlier[:3]:
        print(f"    <- {when}s {label}")
    if len(earlier) > 3:
        print(f"    ... {len(earlier)} places in all")
    for label in stored[:3]:
        print(f"    <- {label}")
    if not earlier and not stored:
        print("    <- not found: generated, or built by a script")


def _print_request(capture: Capture, request: dict) -> None:
    url = urlsplit(request["url"])
    moment = round(request["t"] - capture.start)
    print(f"\n=== {moment}s {request['method']} {request['type']} {url.netloc}{url.path}")
    initiator = request.get("initiator") or {}
    for frame in ((initiator.get("stack") or {}).get("callFrames") or [])[:5]:
        print("  sent by", frame.get("functionName") or "(anonymous)", frame.get("url", "")[:90])
    body = request.get("post")
    if isinstance(body, dict):
        print(f"  body redacted, {body['length']} characters")
    for source, name, value in fields_of(capture, request):
        secret = value.removeprefix("Bearer ")
        kind = shape(secret)
        print(f"  {source} {name}: {len(secret)} characters, {kind}")
        if kind.startswith("word") or len(value) < MIN_TRACED_LENGTH:
            continue
        _print_origins(capture, request, value)


def trace(capture: Capture, pattern: re.Pattern[str]) -> None:
    for request in capture.requests.values():
        if pattern.search(request["url"]):
            _print_request(capture, request)


def overview(capture: Capture) -> None:
    print(Counter(event["kind"] for event in capture.events))
    for event in capture.events:
        if event["kind"] == "download":
            print(f"{round(event['t'] - capture.start)}s download from {urlsplit(event['url'])[0]}")
    for request in capture.requests.values():
        url = urlsplit(request["url"])
        if not ASSET.search(url.path) and LIKELY_DOWNLOAD.search(f"{url.path}?{url.query}"):
            moment = round(request["t"] - capture.start)
            print(f"{moment}s {request['method']} {request['type']} {url.netloc}{url.path}")
    for label, value in sorted({(label, len(text)) for label, text in capture.stored}):
        if TOKEN_NAME.search(label):
            print(f"stored: {label}, {value} characters")


def main() -> None:
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    capture = Capture(Path(sys.argv[1]))
    if len(sys.argv) == 3:
        trace(capture, re.compile(sys.argv[2]))
    else:
        overview(capture)


if __name__ == "__main__":
    main()
