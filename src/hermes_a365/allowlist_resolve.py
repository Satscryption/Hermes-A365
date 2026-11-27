"""``allowlist resolve``: UPN/email to Entra object IDs via the operator's ``az`` login."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from urllib.parse import quote

_OID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
_USERS = "https://graph.microsoft.com/v1.0/users"
_MAIL_TAIL = "&$select=id&$top=2"
_EXIT = {"ambiguous": 2, "unresolvable": 2, "denied": 3, "throttled": 4, "unavailable": 4}
_MARKS = (
    (404, ("Request_ResourceNotFound", "Not Found")),
    (403, ("Authorization_RequestDenied", "Forbidden", "AADSTS")),
    (429, ("TooManyRequests", "Too Many Requests")),
)
Get = Callable[[str], tuple[int, dict]]


def register(subs: argparse._SubParsersAction) -> None:
    p = subs.add_parser("allowlist", help="Sender allowlist utilities")
    resolve = p.add_subparsers(dest="allowlist_command").add_parser("resolve")
    resolve.set_defaults(allowlist_run=run)


def assert_user_lookup_url(url: str) -> None:
    head, _, query = url.partition("?")
    segment = head.removeprefix(_USERS + "/")
    by_id = segment != head and segment and "/" not in segment and query == "$select=id"
    by_mail = head == _USERS and query.startswith("$filter=mail%20eq%20%27")
    if "#" in url or not (by_id or (by_mail and query.endswith(_MAIL_TAIL))):
        raise ValueError("refusing an unsupported Graph user lookup")


def _az_get(url: str) -> tuple[int, dict]:
    assert_user_lookup_url(url)
    argv = ["az", "rest", "--method", "GET", "--uri", url, "--only-show-errors"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return 200, json.loads(proc.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 503, {}
    found = (s for s, marks in _MARKS if any(m in proc.stderr for m in marks))
    return next(found, 503), {}


def _classify(status: int, body: dict) -> tuple[str, str | None]:
    if status != 200:
        return {404: "unresolvable", 403: "denied", 429: "throttled"}.get(
            status, "unavailable"
        ), None
    ids = [str(r.get("id", "")).lower() for r in body.get("value", [body]) if isinstance(r, dict)]
    if len(ids) != 1:
        return ("ambiguous" if ids else "unresolvable"), None
    return ("resolved", ids[0]) if _OID.fullmatch(ids[0]) else ("unavailable", None)


def resolve_entry(entry: str, get: Get) -> tuple[str, str | None]:
    entry = entry.strip().lower()
    if _OID.fullmatch(entry):
        return "resolved", entry
    if "@" not in entry:
        return "unresolvable", None
    state, oid = _classify(*get(f"{_USERS}/{quote(entry, safe='')}?$select=id"))
    if state == "unresolvable" and "#ext#" not in entry.lower():
        mail = quote("mail eq '" + entry.replace("'", "''") + "'", safe="")
        state, oid = _classify(*get(f"{_USERS}?$filter={mail}{_MAIL_TAIL}"))
    return state, oid


def run(_args: argparse.Namespace, *, get: Get = _az_get) -> int:
    entries = [e.strip() for e in sys.stdin.read().splitlines() if e.strip()]
    if not entries:
        print("allowlist resolve: no entries given", file=sys.stderr)
        return 2
    oids: list[str] = []
    for index, entry in enumerate(entries, 1):
        state, oid = resolve_entry(entry, get)
        if state != "resolved" or oid is None:
            print(f"allowlist resolve: entry {index} {state}; no list printed", file=sys.stderr)
            return _EXIT[state]
        if oid not in oids:
            oids.append(oid)
    print("A365_ALLOWED_USERS=" + ",".join(oids))
    return 0
