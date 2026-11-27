"""Offline ``hermes-a365 allowlist resolve`` — map UPN/email to Entra object IDs."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import sys
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote
from uuid import UUID

from .entra_state import _GRAPH_HOST, _odata_string, _select_query, user_lookup_graph_get
from .mutator import get_mutator

_R = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
_SALT = os.urandom(16)
_Lookup = Callable[[str], dict[str, Any]]
_DENY = (
    "Graph denied the user lookup. Ask a tenant admin for read access to directory "
    "users, or copy object IDs from the Entra admin centre."
)
_BAD = "Graph user lookup is unavailable; try again later."


def build_parser(parser: argparse.ArgumentParser | None = None) -> argparse.ArgumentParser:
    p = parser or argparse.ArgumentParser(
        description="Resolve allowlist UPN/email entries (stdin)."
    )
    p.add_argument("--verify", action="store_true", help="verify object IDs are still present")
    return p


def _fp(e: str) -> str:
    return hmac.new(_SALT, e.lower().encode(), hashlib.sha256).hexdigest()[:8]


def _oid(v: Any) -> str | None:
    t = str(v or "").strip().lower()
    return t if _R.fullmatch(t) else None


def _url(kind: str, e: str) -> str:
    if kind == "p":
        principal = f"https://{_GRAPH_HOST}/v1.0/users/{quote(e, safe='')}"
        return f"{principal}?{_select_query('id,userPrincipalName')}"
    if kind == "m":
        f = f"mail eq '{_odata_string(e)}'"
        query = f"$filter={quote(f, safe='')}&{_select_query('id')}&$top=2"
        return f"https://{_GRAPH_HOST}/v1.0/users?{query}"
    return f"https://{_GRAPH_HOST}/v1.0/users/{UUID(e)}?{_select_query('id')}"


def _get(g: _Lookup, url: str, sleep: Callable[[float], None]) -> dict[str, Any]:
    for _ in range(3):
        r = g(url)
        if int(r.get("status") or 503) != 429:
            return r
        sleep(min(int(r.get("retry_after") or 1), 60))
    return {"status": 503, "error_code": "Unavailable"}


def _read(r: dict[str, Any]) -> tuple[str, str | None]:
    s, c = int(r.get("status") or 503), str(r.get("error_code") or "")
    if s == 403 or c.startswith("AADSTS"):
        return "denied", None
    if s in (429, 503) or s >= 500:
        return "unavailable", None
    if s == 404:
        return "unresolvable", None
    if s != 200:
        return "unavailable", None
    if o := _oid(r.get("id")):
        return "resolved", o
    ids = [x for x in (_oid(i) for i in (r.get("ids") or ())) if x]
    if len(ids) == 1:
        return "resolved", ids[0]
    return ("ambiguous", None) if len(ids) > 1 else ("unavailable", None)


def _fail(counts: dict[str, int], fails: list[tuple[int, str, str]]) -> None:
    if line := ", ".join(f"{k}={v}" for k, v in sorted(counts.items()) if v):
        print(f"allowlist resolve: {line}", file=sys.stderr)
    for i, d, t in fails:
        print(f"entry index={i} fingerprint={d} status={t}", file=sys.stderr)


def run(
    args: argparse.Namespace,
    *,
    getter: _Lookup | None = None,
    sleep: Callable[[float], None] | None = None,
) -> int:
    lines = [x.rstrip("\n") for x in sys.stdin]
    if not any(x.strip() for x in lines):
        print("stdin must list one allowlist entry per line", file=sys.stderr)
        return 2
    g, sleep = getter or user_lookup_graph_get(get_mutator()), sleep or time.sleep
    counts, fails, seen, out = {}, [], set(), []
    for i, raw in enumerate(lines, 1):
        e, name = raw.strip(), ""
        if args.verify:
            oid = _oid(e)
            if not oid:
                try:
                    oid = str(UUID(e)).lower()
                except ValueError:
                    counts["stale"] = counts.get("stale", 0) + 1
                    fails.append((i, _fp(raw or e), "stale"))
                    continue
            name, _ = _read(_get(g, _url("i", oid), sleep))
            counts[name] = counts.get(name, 0) + 1
            if name == "unresolvable":
                fails.append((i, _fp(oid), "stale"))
        elif "@" not in e:
            if not (oid := _oid(e)):
                counts["unresolvable"] = counts.get("unresolvable", 0) + 1
                fails.append((i, _fp(e), "unresolvable"))
                continue
            counts["kept-oid"] = counts.get("kept-oid", 0) + 1
            if oid not in seen:
                seen.add(oid)
                out.append(oid)
            continue
        else:
            name, oid = _read(_get(g, _url("p", e), sleep))
            if name == "unresolvable":
                name, oid = _read(_get(g, _url("m", e), sleep))
            counts[name] = counts.get(name, 0) + 1
            if name == "resolved" and oid and oid not in seen:
                seen.add(oid)
                out.append(oid)
            elif name in ("unresolvable", "ambiguous"):
                fails.append((i, _fp(e), name))
        if name in ("denied", "unavailable"):
            _fail(counts, fails)
            print(_DENY if name == "denied" else _BAD, file=sys.stderr)
            return 3 if name == "denied" else 4
    _fail(counts, fails)
    if fails:
        return 2
    if not args.verify:
        print("A365_ALLOWED_USERS=" + ",".join(out))
    return 0
